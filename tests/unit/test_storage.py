"""원본 저장소·PIT 파일 저장소·수집 기록 (가이드 6-1, 6-2)."""
import gzip
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from collectors.base_client import HttpResponse
from core.data_contracts import PermissionDenied, Purpose
from core.pit_schema import PITRecord
from storage.manifests import BatchManifest
from storage.pit_store import PITFileStore, by_period_month, record_from_json, record_to_json
from storage.raw_store import RawStore, sha256_hex
from tests.conftest import make_contract

UTC = timezone.utc
T0 = datetime(2026, 11, 9, 0, 30, tzinfo=UTC)


def response(body=b'{"status":"000"}', params=None, at=T0):
    return HttpResponse(url="https://api.example.invalid/api/list.json", params=params or {"bgn_de": "20261106"},
                        status=200, body=body, headers={}, attempts=1, requested_at_utc=at, received_at_utc=at)


# ---------- 원본 저장소 ----------

def test_raw_store_is_content_addressed_and_logs_every_fetch(tmp_path, registry):
    store = RawStore(tmp_path, registry)
    a = store.put(contract_id="kr_price", source_id="src", response=response(), request_id="req-1")
    b = store.put(contract_id="kr_price", source_id="src", response=response(at=T0 + timedelta(days=1)))
    assert a.sha256 == b.sha256 == sha256_hex(b'{"status":"000"}') and a.is_new and not b.is_new
    assert len(list((tmp_path / "raw" / "src" / "objects").rglob("*.gz"))) == 1     # 내용은 한 번만
    entries = list(store.index_entries("src"))
    assert [e["received_at_utc"][:10] for e in entries] == ["2026-11-09", "2026-11-10"] and entries[0]["request_id"] == "req-1"
    assert store.get("src", a.sha256) == b'{"status":"000"}'


def test_raw_store_redacts_secrets_again(tmp_path, registry):
    store = RawStore(tmp_path, registry)
    store.put(contract_id="kr_price", source_id="src", response=response(params={"crtfc_key": "LEAK", "x": "1"}))
    text = "".join(p.read_text() for p in (tmp_path / "raw").rglob("*.jsonl"))
    assert "LEAK" not in text and '"crtfc_key": "***"' in text


def test_raw_store_refuses_without_storage_permission(tmp_path, registry):
    store = RawStore(tmp_path, registry)
    for contract in ("unreviewed", "missing"):
        with pytest.raises(PermissionDenied):
            store.put(contract_id=contract, source_id="src", response=response())
    assert not (tmp_path / "raw").exists()


def test_raw_store_detects_tampering(tmp_path, registry):
    store = RawStore(tmp_path, registry)
    ref = store.put(contract_id="kr_price", source_id="src", response=response())
    (tmp_path / ref.relpath).write_bytes(gzip.compress(b"tampered"))
    with pytest.raises(ValueError, match="해시"):
        store.get("src", ref.sha256)
    with pytest.raises(ValueError, match="손상"):
        store.put(contract_id="kr_price", source_id="src", response=response())


# ---------- PIT 파일 저장소 ----------

def rec(revision_id="r1", value=100.0, available=T0, contract="kr_price", **kw):
    base = dict(entity_id="00126380", series_id="dart_fin", metric_id="revenue", available_at_utc=available,
                ingested_at_utc=available, revision_id=revision_id, value=value, unit="KRW", source_provider="DART",
                raw_payload_hash="h", license_contract_id=contract, observation_period_start=date(2026, 7, 1),
                observation_period_end=date(2026, 9, 30))
    base.update(kw)
    return PITRecord(**base)


def test_json_roundtrip_keeps_every_field():
    r = rec(event_time_utc=T0, vintage_date=date(2026, 11, 9), quality_status="suspect", currency="KRW")
    assert record_from_json(json.loads(json.dumps(record_to_json(r)))) == r


def test_pit_file_store_appends_and_reloads(tmp_path, registry):
    fs = PITFileStore(tmp_path, registry)
    assert fs.append([rec("r1", 100.0)]).added == 1
    # 다음 달에 정정본
    later = T0 + timedelta(days=40)
    assert fs.append([rec("r2", 105.0, later, supersedes_revision_id="r1")]).added == 1
    assert [p.name for p in (tmp_path / "pit_versions" / "dart_fin").glob("*.jsonl")] == ["00126380.jsonl"]   # 원본·정정본이 한 파일

    store = PITFileStore(tmp_path, registry).load()
    assert store.query_as_of(T0 + timedelta(days=1), Purpose.ML_TRAINING)["value"].tolist() == [100.0]
    assert store.query_as_of(later, Purpose.ML_TRAINING)["value"].tolist() == [105.0]


def test_reingest_is_idempotent_and_changed_values_are_conflicts(tmp_path, registry):
    fs = PITFileStore(tmp_path, registry)
    fs.append([rec("r1", 100.0)])
    again = fs.append([rec("r1", 100.0, ingested_at_utc=T0 + timedelta(days=1))])        # 다음 날 다시 받음
    assert (again.added, again.unchanged, again.conflicts) == (0, 1, ())
    # 같은 값이 다른 원본 응답(예: 조회 기간이 밀린 공시 목록 페이지)에 실려 와도 같은 버전이다
    other_page = fs.append([rec("r1", 100.0, ingested_at_utc=T0 + timedelta(days=2), raw_payload_hash="h2")])
    assert (other_page.added, other_page.unchanged, other_page.conflicts) == (0, 1, ())
    changed = fs.append([rec("r1", 999.0), rec("r3", 1.0, metric_id="op_income")])
    assert changed.added == 1 and changed.conflicts == ("r1",)
    values = {r.revision_id: r.value for r in fs.iter_records()}
    assert values == {"r1": 100.0, "r3": 1.0}                                             # 덮어쓰지 않음


def test_invalid_batch_writes_nothing(tmp_path, registry):
    fs = PITFileStore(tmp_path, registry)
    with pytest.raises(PermissionError):
        fs.append([rec("ok"), rec("bad", contract="unreviewed", metric_id="x")])
    with pytest.raises(ValueError, match="없는 revision"):
        fs.append([rec("ok"), rec("bad", supersedes_revision_id="nope", metric_id="x")])
    assert list(fs.iter_records()) == []


def test_partition_by_period_month_and_selective_load(tmp_path, registry):
    fs = PITFileStore(tmp_path, registry, partitions={"filings": by_period_month})
    fs.append([rec("a", series_id="filings", observation_period_start=date(2026, 10, 30), observation_period_end=date(2026, 10, 30)),
               rec("b", series_id="filings", observation_period_start=date(2026, 11, 2), observation_period_end=date(2026, 11, 2))])
    assert sorted(p.name for p in (tmp_path / "pit_versions" / "filings").glob("*.jsonl")) == ["2026-10.jsonl", "2026-11.jsonl"]
    assert [r.revision_id for r in fs.iter_records(parts=[("filings", "2026-11")])] == ["b"]   # 필요한 파일만 읽음
    store = fs.load(["filings"])
    assert store.latest(rec("x", series_id="filings", observation_period_start=date(2026, 11, 2),
                            observation_period_end=date(2026, 11, 2)).observation_key).revision_id == "b"


def test_series_id_must_be_a_plain_name(tmp_path, registry):
    with pytest.raises(ValueError, match="series_id"):
        PITFileStore(tmp_path, registry).append([rec(series_id="../escape")])


# ---------- 수집 기록 ----------

def test_manifest_status_and_file(tmp_path):
    c = make_contract("dart_list", allow=(Purpose.STORAGE,))
    m = BatchManifest.start(run_id="run-1", contract=c, code_version="abc", target={"bgn_de": "20261106"}, now=T0)
    m.count("added", 3)
    m.finish(now=T0 + timedelta(minutes=1))
    assert m.status == "ok"
    path = m.write(tmp_path)
    data = json.loads(path.read_text())
    assert path.relative_to(tmp_path).as_posix() == "manifests/src/2026-11-09/run-1.json"
    assert data["counts"] == {"added": 3} and data["permission"]["approval_status"] == "approved_for_training"

    m2 = BatchManifest.start(run_id="run-2", contract=c, code_version="abc", target={}, now=T0)
    m2.errors.append("HTTP 500")
    m2.finish()
    assert m2.status == "failed"
    m2.count("unchanged")
    m2.finish()
    assert m2.status == "partial"
