"""OpenDART 매일 수집 흐름을 가짜 서버로 여러 날에 걸쳐 확인한다 (네트워크 없음)."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from collectors.base_client import CallBudget, TokenBucket
from collectors.opendart import OpenDartClient
from core.data_contracts import Purpose
from core.source_registry import default_registry
from orchestration import opendart_daily
from orchestration.opendart_daily import DailyRun, FsState
from storage.pit_store import PITFileStore
from tests.fake_opendart import TEST_KEY, FakeDart

UTC = timezone.utc
SAMSUNG, KOSDAQ_CO = "00126380", "00999999"


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make_run(fake, tmp_path, clock, max_calls=500):
    c = OpenDartClient(TEST_KEY, budget=CallBudget(max_calls), rate=TokenBucket(1e9), transport=fake, sleep=lambda s: None,
                       now=clock)
    return DailyRun(c, tmp_path, default_registry(), now=clock, version="test")


def accounts(op):
    return [("IS", "ifrs-full_Revenue", "매출액", "100,000", "300,000"), ("IS", "dart_OperatingIncomeLoss", "영업이익", op, None),
            ("BS", "ifrs-full_Assets", "자산총계", "1,000,000", None)]


@pytest.fixture
def world():
    fake = FakeDart()
    fake.filing("20261106000100", SAMSUNG, "분기보고서 (2026.09)", rm="")      # 실제 데이터처럼 비고에 '연'이 없어도 연결을 묻는다
    fake.filing("20261106000200", SAMSUNG, "주요사항보고서(자기주식취득결정)")
    fake.filing("20261106000300", KOSDAQ_CO, "분기보고서 (2026.09)", corp_cls="K", rm="코")
    return fake


def test_daily_runs_over_several_days(world, tmp_path):
    fake = world
    clock = Clock(datetime(2026, 11, 8, 21, 17, tzinfo=UTC))      # 11/9(월) 06:17 KST

    # 1일차: 공시 목록은 저장, 재무제표 API는 아직 비어 있음 → 대기
    res = make_run(fake, tmp_path, clock).run(end=date(2026, 11, 8), lookback_days=3)
    assert not res.failed and res.list_manifest.counts["added"] == 3
    assert res.fs_manifest.counts == {"not_yet": 1}                      # 코스닥 보고서는 재무제표 대상 아님
    assert list(FsState.load(tmp_path).pending) == ["20261106000100"]

    # 2일차: 재무제표가 올라옴 (별도·연결)
    fake.statement("20261106000100", SAMSUNG, 2026, "11014", "OFS", accounts("10,000"))
    fake.statement("20261106000100", SAMSUNG, 2026, "11014", "CFS", accounts("12,000"))
    clock.t += timedelta(days=1)
    res = make_run(fake, tmp_path, clock).run(end=date(2026, 11, 9), lookback_days=4)
    assert not res.failed and res.list_manifest.counts["unchanged"] == 3 and res.list_manifest.counts.get("added", 0) == 0
    assert res.fs_manifest.counts["reports"] == 1 and res.fs_manifest.counts["added"] == 8      # (4개 값) × 별도·연결
    st = FsState.load(tmp_path)
    assert not st.pending and st.done["20261106000100"]["fs_divs"] == ["OFS", "CFS"]

    # 3일차: 비고에 '정'이 붙고, [기재정정] 보고서가 들어옴 — 연결 영업이익만 바뀜
    fake.filing("20261106000100", SAMSUNG, "분기보고서 (2026.09)", rm="정")
    fake.filing("20261110000400", SAMSUNG, "[기재정정]분기보고서 (2026.09)", rm="")
    fake.statement("20261110000400", SAMSUNG, 2026, "11014", "OFS", accounts("10,000"))
    fake.statement("20261110000400", SAMSUNG, 2026, "11014", "CFS", accounts("11,000"))
    clock.t += timedelta(days=1)
    res = make_run(fake, tmp_path, clock).run(end=date(2026, 11, 10), lookback_days=5)
    assert res.list_manifest.counts["added"] == 2                         # 바뀐 비고 + 정정 공시
    assert res.fs_manifest.counts["added"] == 1 and res.fs_manifest.counts["unchanged"] == 7
    # 이미 맞는 (연도, 코드)를 알아서 후보를 하나만 물었다
    fs_calls = [c for c in fake.calls if c[0] == "fnlttSinglAcntAll.json" and c[1]["corp_code"] == SAMSUNG][-2:]
    assert [(c[1]["bsns_year"], c[1]["reprt_code"], c[1]["fs_div"]) for c in fs_calls] == [("2026", "11014", "OFS"), ("2026", "11014", "CFS")]

    store = PITFileStore(tmp_path, default_registry()).load()
    key = ("dart_fs", "CFS|IS|dart_OperatingIncomeLoss|영업이익|당기")

    def cfs_op(as_of):
        q = store.query_as_of(as_of, Purpose.ML_TRAINING, entity_id=SAMSUNG, series_id=key[0])
        return q.loc[q.metric_id == key[1], "value"].tolist()

    assert cfs_op(datetime(2026, 11, 9, 23, 30, tzinfo=UTC)) == [12000]      # 2일차에 받은 값
    assert cfs_op(datetime(2026, 11, 10, 23, 29, tzinfo=UTC)) == [12000]     # 정정본은 아직 쓸 수 없음
    assert cfs_op(datetime(2026, 11, 10, 23, 30, tzinfo=UTC)) == [11000]     # 11/11 08:30 KST부터 정정값
    assert cfs_op(datetime(2026, 11, 9, 23, 29, tzinfo=UTC)) == []           # 받기 전에는 없음

    filings = store.query_as_of(datetime(2026, 11, 10, 23, 30, tzinfo=UTC), Purpose.PUBLIC_DISPLAY, series_id="dart_filings")
    rm = {r.source_document_id: json.loads(r.value)["rm"] for r in filings.itertuples()}
    assert rm["20261106000100"] == "정" and len(rm) == 4

    # 저장된 어떤 파일에도 인증키가 없다
    for p in tmp_path.rglob("*"):
        if p.is_file() and p.suffix in (".json", ".jsonl"):
            assert TEST_KEY not in p.read_text(encoding="utf-8")
    assert len(list((tmp_path / "manifests").rglob("*.json"))) == 9      # 3일 × (공시·재무제표·XBRL)


def test_done_items_without_cfs_check_are_requeued(world, tmp_path):
    """연결재무제표를 묻지 않고 끝냈던 항목(이전 버전)은 다음 실행에서 연결만 새로 받는다."""
    fake, clock = world, Clock(datetime(2026, 11, 8, 21, 17, tzinfo=UTC))
    fake.statement("20261106000100", SAMSUNG, 2026, "11014", "OFS", accounts("10,000"))
    make_run(fake, tmp_path, clock).run(end=date(2026, 11, 8), lookback_days=3)
    st = FsState.load(tmp_path)
    assert st.done["20261106000100"]["fs_divs"] == ["OFS"] and st.done["20261106000100"]["cfs_checked"]
    # 이전 버전이 남긴 상태처럼 cfs_checked를 지운다
    d = st.done["20261106000100"]
    del d["cfs_checked"], d["rcept_dt"]
    st.save(tmp_path)
    fake.statement("20261106000100", SAMSUNG, 2026, "11014", "CFS", accounts("12,000"))
    clock.t += timedelta(days=1)
    res = make_run(fake, tmp_path, clock).run(end=date(2026, 11, 9), lookback_days=1)
    assert res.fs_manifest.counts["added"] == 4 and res.fs_manifest.counts["unchanged"] == 4
    assert FsState.load(tmp_path).done["20261106000100"]["fs_divs"] == ["OFS", "CFS"]


def test_fatal_status_stops_run_and_marks_failure(world, tmp_path):
    world.force_status = "020"
    res = make_run(world, tmp_path, Clock(datetime(2026, 11, 8, 21, 17, tzinfo=UTC))).run(end=date(2026, 11, 8), lookback_days=3)
    assert res.failed and res.stopped == "OpenDART status 020" and res.fs_manifest is None
    assert res.list_manifest.status == "failed"


def test_budget_leaves_work_for_next_run(world, tmp_path):
    clock = Clock(datetime(2026, 11, 8, 21, 17, tzinfo=UTC))
    res = make_run(world, tmp_path, clock, max_calls=4).run(end=date(2026, 11, 8), lookback_days=3)   # 목록 2회 + 여유 부족
    assert not res.failed and "다음 실행" in res.fs_manifest.warnings[0]
    assert list(FsState.load(tmp_path).pending) == ["20261106000100"]


def test_pending_report_is_dropped_after_give_up_days(world, tmp_path):
    clock = Clock(datetime(2026, 11, 8, 21, 17, tzinfo=UTC))
    make_run(world, tmp_path, clock).run(end=date(2026, 11, 8), lookback_days=3)
    clock.t += timedelta(days=opendart_daily.GIVE_UP_DAYS)
    res = make_run(world, tmp_path, clock).run(end=date(2026, 11, 21), lookback_days=1)
    st = FsState.load(tmp_path)
    assert not st.pending and "20261106000100" in st.gave_up and any("대기열에서 뺌" in w for w in res.fs_manifest.warnings)


def test_cli_requires_key_and_check_mode(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("DART_API_KEY", raising=False)
    assert opendart_daily.main(["--data-dir", str(tmp_path)]) == 2
    assert "DART_API_KEY" in capsys.readouterr().err


# ---------- XBRL 사업부문 단계 ----------

def test_xbrl_stage_stores_segment_numbers_but_not_the_raw_zip(world, tmp_path):
    from collectors.opendart_segments import SERIES_SEGMENTS
    from feature_store.segment_exposure import compute_exposure
    from tests.unit.test_segments import samsung_like

    fake = world
    fake.statement("20261106000100", SAMSUNG, 2026, "11014", "OFS", accounts("10,000"))
    clock = Clock(datetime(2026, 11, 8, 21, 17, tzinfo=UTC))
    res = make_run(fake, tmp_path, clock).run(end=date(2026, 11, 8), lookback_days=3)
    assert res.xbrl_manifest.counts == {"not_yet": 1} and not res.failed            # XBRL 아직 없음(013)

    fake.xbrl["20261106000100"] = samsung_like()
    clock.t += timedelta(days=1)
    res = make_run(fake, tmp_path, clock).run(end=date(2026, 11, 9), lookback_days=4)
    assert res.xbrl_manifest.counts["reports"] == 1 and res.xbrl_manifest.counts["segment_ok"] == 1
    req = res.xbrl_manifest.requests[0]
    assert req["segment"] == "OK" and req["values"] == 12 and TEST_KEY not in json.dumps(req)
    st = opendart_daily.XbrlState.load(tmp_path)
    assert st.done["20261106000100"]["segment"] == "OK" and not st.attempts

    # 원본 zip은 어디에도 저장하지 않는다 (서술형 주석 포함 → 계약상 저장·재배포 안 함)
    assert not (tmp_path / "raw" / "opendart_xbrl").exists()
    for p in tmp_path.rglob("*"):
        if p.is_file():
            assert not p.read_bytes().startswith(b"PK"), p

    store = PITFileStore(tmp_path, default_registry()).load([SERIES_SEGMENTS])
    rep = compute_exposure(store, SAMSUNG, datetime(2026, 11, 10, 23, 30, tzinfo=UTC))
    assert rep.industry_revenue_weights["semiconductor"] == pytest.approx(130 / 364)

    # 다음 날 다시 돌려도 다시 받지 않는다
    clock.t += timedelta(days=1)
    res = make_run(fake, tmp_path, clock).run(end=date(2026, 11, 10), lookback_days=1)
    assert res.xbrl_manifest.counts == {} and res.xbrl_manifest.target["queued"] == 0


def test_xbrl_time_budget_and_give_up(world, tmp_path):
    fake = world
    fake.statement("20261106000100", SAMSUNG, 2026, "11014", "OFS", accounts("10,000"))
    clock = Clock(datetime(2026, 11, 8, 21, 17, tzinfo=UTC))
    # 시간 예산 0분이면 XBRL 단계를 건너뛴다
    res = make_run(fake, tmp_path, clock).run(end=date(2026, 11, 8), lookback_days=3, xbrl_minutes=0)
    assert res.xbrl_manifest is None
    # XBRL이 계속 없으면(013) XBRL_MAX_ATTEMPTS번 뒤 포기하고 경고
    for _ in range(opendart_daily.XBRL_MAX_ATTEMPTS):
        clock.t += timedelta(days=1)
        res = make_run(fake, tmp_path, clock).run(end=date(2026, 11, 8), lookback_days=3)
    st = opendart_daily.XbrlState.load(tmp_path)
    assert "20261106000100" in st.gave_up and any("포기" in w for w in res.xbrl_manifest.warnings)


def test_xbrl_unparseable_payload_is_given_up(world, tmp_path):
    fake = world
    fake.statement("20261106000100", SAMSUNG, 2026, "11014", "OFS", accounts("10,000"))
    fake.xbrl["20261106000100"] = b"PK\x03\x04broken"
    res = make_run(fake, tmp_path, Clock(datetime(2026, 11, 8, 21, 17, tzinfo=UTC))).run(end=date(2026, 11, 8), lookback_days=3)
    assert res.xbrl_manifest.counts == {"gave_up": 1} and "해석 실패" in res.xbrl_manifest.warnings[0]
