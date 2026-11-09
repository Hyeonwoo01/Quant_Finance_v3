"""PIT 레코드와 as-of 조회 (가이드 4-1, 4-2)."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from core.data_contracts import Purpose
from core.pit_schema import PITRecord, PITStore, QualityStatus

UTC = timezone.utc
KST = timezone(timedelta(hours=9))


def rec(revision_id="r1", value=100.0, available=datetime(2026, 11, 9, 0, 0, tzinfo=UTC), contract="kr_price", **kw):
    base = dict(entity_id="005930", series_id="dart_fin", metric_id="revenue", available_at_utc=available,
                ingested_at_utc=available + timedelta(minutes=5), revision_id=revision_id, value=value, unit="KRW",
                source_provider="DART", raw_payload_hash="h", license_contract_id=contract,
                observation_period_start=date(2026, 7, 1), observation_period_end=date(2026, 9, 30))
    base.update(kw)
    return PITRecord(**base)


def test_naive_times_are_rejected_and_aware_times_become_utc():
    with pytest.raises(ValueError, match="시간대"):
        rec(available=datetime(2026, 11, 9, 9, 0))
    r = rec(available=datetime(2026, 11, 9, 8, 30, tzinfo=KST))
    assert r.available_at_utc == datetime(2026, 11, 8, 23, 30, tzinfo=UTC)


def test_available_at_cannot_precede_release_or_provider_time():
    t = datetime(2026, 11, 9, tzinfo=UTC)
    with pytest.raises(ValueError, match="시간 역전"):
        rec(available=t, official_release_at_utc=t + timedelta(hours=1))
    with pytest.raises(ValueError, match="시간 역전"):
        rec(available=t, provider_available_at_utc=t + timedelta(seconds=1))
    assert rec(available=t, official_release_at_utc=t).available_at_utc == t  # 같은 시각은 허용


def test_observation_period_order():
    with pytest.raises(ValueError, match="observation_period"):
        rec(observation_period_start=date(2026, 9, 30), observation_period_end=date(2026, 7, 1))


def test_revision_as_of_returns_version_known_at_that_time(registry):
    t0 = datetime(2026, 11, 9, tzinfo=UTC)
    store = PITStore(registry)
    store.append(rec("r1", 100.0, t0))
    store.append(rec("r2", 105.0, t0 + timedelta(days=30), supersedes_revision_id="r1"))

    before = store.query_as_of(t0 + timedelta(days=1), Purpose.ML_TRAINING)
    after = store.query_as_of(t0 + timedelta(days=30), Purpose.ML_TRAINING)
    assert before["value"].tolist() == [100.0] and before["revision_id"].tolist() == ["r1"]
    assert after["value"].tolist() == [105.0] and after["revision_id"].tolist() == ["r2"]
    assert store.query_as_of(t0 - timedelta(seconds=1), Purpose.ML_TRAINING).empty
    assert len(store) == 2  # 원본은 덮어쓰지 않고 남는다


def test_revision_rules(registry):
    t0 = datetime(2026, 11, 9, tzinfo=UTC)
    store = PITStore(registry)
    store.append(rec("r1", 100.0, t0))
    with pytest.raises(ValueError, match="이미 있는 revision_id"):
        store.append(rec("r1", 101.0, t0 + timedelta(days=1)))
    with pytest.raises(ValueError, match="없는 revision"):
        store.append(rec("r2", 101.0, t0 + timedelta(days=1), supersedes_revision_id="nope"))
    with pytest.raises(ValueError, match="같은 관측값"):
        store.append(rec("r2", 101.0, t0 + timedelta(days=1), supersedes_revision_id="r1", metric_id="op_income"))
    with pytest.raises(ValueError, match="먼저 이용 가능"):
        store.append(rec("r2", 101.0, t0 - timedelta(days=1), supersedes_revision_id="r1"))


def test_storage_permission_is_required_to_append(registry):
    store = PITStore(registry)
    for contract in ("unreviewed", "no_such_contract"):
        with pytest.raises(PermissionError):
            store.append(rec("r1", contract=contract))
    assert len(store) == 0


def test_query_filters_by_purpose(registry):
    t0 = datetime(2026, 11, 9, tzinfo=UTC)
    store = PITStore(registry)
    store.append(rec("a", contract="kr_price", entity_id="005930"))
    store.append(rec("b", contract="store_only", entity_id="000660"))
    as_of = t0 + timedelta(days=1)
    assert store.query_as_of(as_of, Purpose.STORAGE)["entity_id"].tolist() == ["000660", "005930"]
    assert store.query_as_of(as_of, Purpose.ML_TRAINING)["entity_id"].tolist() == ["005930"]  # 학습 권한 없는 계약은 제외
    assert store.query_as_of(as_of, Purpose.LLM_INPUT).empty


def test_query_filters_by_quality_and_entity(registry):
    t0 = datetime(2026, 11, 9, tzinfo=UTC)
    store = PITStore(registry)
    store.append(rec("ok", entity_id="005930"))
    store.append(rec("sus", entity_id="000660", quality_status=QualityStatus.SUSPECT))
    store.append(rec("bad", entity_id="035420", quality_status="invalid"))
    as_of = t0 + timedelta(days=1)
    assert store.query_as_of(as_of, Purpose.STORAGE)["revision_id"].tolist() == ["ok"]
    assert set(store.query_as_of(as_of, Purpose.STORAGE, include_suspect=True)["revision_id"]) == {"ok", "sus"}
    assert store.query_as_of(as_of, Purpose.STORAGE, entity_id="000660", include_suspect=True)["revision_id"].tolist() == ["sus"]


def test_query_rejects_naive_as_of(registry):
    with pytest.raises(ValueError, match="시간대"):
        PITStore(registry).query_as_of(datetime(2026, 11, 9), Purpose.STORAGE)


def test_empty_query_keeps_schema(registry):
    out = PITStore(registry).query_as_of(datetime(2026, 11, 9, tzinfo=UTC), Purpose.STORAGE)
    assert out.empty and "available_at_utc" in out.columns and "revision_id" in out.columns


def test_records_are_immutable():
    r = rec()
    with pytest.raises(Exception):
        r.value = 1.0  # type: ignore[misc]
    assert replace(r, value=1.0).value == 1.0 and r.value == 100.0
