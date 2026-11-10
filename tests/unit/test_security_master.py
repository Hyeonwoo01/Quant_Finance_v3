"""종목 식별자 매핑: 고유번호 파일 → 시점별 매핑 (가이드 4-8). 가짜 OpenDART로 여러 날을 흉내 낸다."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from collectors import opendart_corpcode
from collectors.base_client import CallBudget, TokenBucket
from collectors.opendart import OpenDartClient, OpenDartError
from collectors.opendart_corpcode import CorpCodeError, SERIES_CORP_MASTER, fetch_corp_codes, parse_corp_codes
from core.security_master import MapStatus, SecurityMaster
from core.source_registry import default_registry
from orchestration.opendart_daily import DailyRun
from storage.pit_store import PITFileStore
from tests.fake_opendart import TEST_KEY, FakeDart, corp_code_zip

UTC = timezone.utc
SAMSUNG, HYNIX, UNLISTED, NEWCO = "00126380", "00164779", "00999001", "00999002"
T0 = datetime(2026, 11, 9, 21, 17, tzinfo=UTC)                 # 11/10(화) 06:17 KST
CUT0 = datetime(2026, 11, 9, 23, 30, tzinfo=UTC)               # 그날 08:30 KST 컷오프


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def small_file_ok(monkeypatch):
    monkeypatch.setattr(opendart_corpcode, "MIN_LISTED", 2)     # 테스트 파일은 회사가 몇 개뿐


@pytest.fixture
def world(tmp_path):
    fake, clock = FakeDart(), Clock(T0)
    fake.corps = [(SAMSUNG, "삼성전자", "005930", "20260301"), (HYNIX, "SK하이닉스", "000660", "20260401"),
                  (UNLISTED, "비상장회사", "", "20250101")]
    client = OpenDartClient(TEST_KEY, budget=CallBudget(50), rate=TokenBucket(1e9), transport=fake,
                            sleep=lambda s: None, now=clock)
    return fake, clock, DailyRun(client, tmp_path, default_registry(), now=clock, version="test"), tmp_path


def master(tmp_path) -> SecurityMaster:
    store = PITFileStore(tmp_path, default_registry(), partitions={SERIES_CORP_MASTER: lambda r: "all"})
    return SecurityMaster.from_records(store.iter_records([SERIES_CORP_MASTER]))


def test_parse_and_fetch(world):
    fake, _, run, _ = world
    f = fetch_corp_codes(run.client)
    assert len(f.rows) == 3 and f.rows[2]["stock_code"] == "" and len(f.zip_sha256) == 64
    with pytest.raises(CorpCodeError):
        parse_corp_codes(b"<result><list><corp_code>1</corp_code></list><list><corp_code>1</corp_code></list></result>")
    fake.force_status = "020"                                   # 오류 응답은 zip이 아니다
    with pytest.raises(OpenDartError) as e:
        fetch_corp_codes(run.client)
    assert e.value.status == "020"


def test_first_run_stores_listed_only_and_past_is_unknown(world):
    fake, clock, run, tmp = world
    m = run.collect_corp_master("r1")
    assert m.status == "ok" and m.counts["new"] == 2 and m.counts["with_code"] == 2 and m.counts["added"] == 2
    assert m.requests[0]["raw_stored"] is False and not (tmp / "raw" / "opendart_corpcode").exists()
    sm = master(tmp)
    assert len(sm) == 2 and sm.coverage_start_utc == CUT0
    r = sm.code_of(SAMSUNG, CUT0)
    assert r.ok and r.mapping.stock_code == "005930" and r.mapping.source_contract_id == "opendart_corpcode_v1"
    assert sm.entity_of("000660", CUT0 + timedelta(days=30)).mapping.entity_id == HYNIX
    # 수집 시작 전 시점은 지금 종목코드로 채우지 않는다
    assert sm.code_of(SAMSUNG, CUT0 - timedelta(seconds=1)).status is MapStatus.BEFORE_COVERAGE
    assert sm.entity_of("005930", datetime(2020, 1, 2, tzinfo=UTC)).status is MapStatus.BEFORE_COVERAGE
    assert sm.code_of(UNLISTED, CUT0).status is MapStatus.UNKNOWN and sm.entity_of("999999", CUT0).status is MapStatus.UNKNOWN
    with pytest.raises(ValueError):
        sm.code_of(SAMSUNG, datetime(2026, 11, 10))            # 시간대 없는 시각은 받지 않는다
    # 다음 날 같은 파일 → 새 버전 없음
    clock.t = T0 + timedelta(days=1)
    m2 = run.collect_corp_master("r2")
    assert m2.counts["added"] == 0 and m2.counts["unchanged"] == 2


def test_changes_delisting_new_listing_and_code_reuse(world):
    fake, clock, run, tmp = world
    run.collect_corp_master("r1")
    # 2일차: 하이닉스 종목코드 변경, 삼성전자 이름 변경, 새 상장사
    clock.t = T0 + timedelta(days=1)
    fake.corps = [(SAMSUNG, "삼성전자주식회사", "005930", "20261110"), (HYNIX, "SK하이닉스", "000661", "20261110"),
                  (NEWCO, "새회사", "123450", "20261110"), (UNLISTED, "비상장회사", "", "20250101")]
    m = run.collect_corp_master("r2")
    assert (m.counts["changed"], m.counts["new"], m.counts["added"]) == (2, 1, 3)
    # 3일차: 하이닉스가 종목코드를 잃고(상장폐지), 새 회사가 그 옛 코드 000660을 쓴다. 삼성전자는 파일에서 사라짐
    clock.t = T0 + timedelta(days=2)
    fake.corps = [(HYNIX, "SK하이닉스", "", "20261111"), (NEWCO, "새회사", "000660", "20261111"),
                  (UNLISTED, "비상장회사", "", "20250101"), ("00999003", "채움1", "111110", "20261111"),
                  ("00999004", "채움2", "111120", "20261111")]
    opendart_corpcode.MAX_DROP_RATIO = 0.9
    try:
        m = run.collect_corp_master("r3")
    finally:
        opendart_corpcode.MAX_DROP_RATIO = 0.2
    assert m.counts["code_removed"] == 2 and m.counts["changed"] == 1 and m.counts["new"] == 2

    sm = master(tmp)
    d1, d2, d3 = CUT0, CUT0 + timedelta(days=1), CUT0 + timedelta(days=2)
    hist = sm.history(HYNIX)
    assert [(h.stock_code, h.valid_from_utc, h.valid_to_utc) for h in hist] == \
        [("000660", d1, d2), ("000661", d2, d3), ("", d3, None)]
    assert sm.code_of(HYNIX, d2 - timedelta(seconds=1)).mapping.stock_code == "000660"
    assert sm.code_of(HYNIX, d2).mapping.stock_code == "000661"
    assert sm.code_of(HYNIX, d3).status is MapStatus.NO_CODE
    # 같은 종목코드라도 시점에 따라 다른 회사
    assert sm.entity_of("000660", d1).mapping.entity_id == HYNIX
    assert sm.entity_of("000660", d2).status is MapStatus.NO_CODE          # 그날은 아무도 안 씀
    assert sm.entity_of("000660", d3).mapping.entity_id == NEWCO
    assert sm.code_of(NEWCO, d1).status is MapStatus.NO_CODE               # 표에 나타나기 전
    # 이름만 바뀐 것도 버전으로 남고, 파일에서 사라진 회사는 종목코드가 빈 버전으로 끝난다
    s = sm.history(SAMSUNG)
    assert [h.name for h in s] == ["삼성전자", "삼성전자주식회사", "삼성전자주식회사"] and s[-1].stock_code == ""
    store = PITFileStore(tmp, default_registry(), partitions={SERIES_CORP_MASTER: lambda r: "all"})
    last = [r for r in store.iter_records([SERIES_CORP_MASTER]) if r.entity_id == SAMSUNG][-1]
    assert json.loads(last.attributes) == {"removed_from_file": True} and last.supersedes_revision_id


def test_code_returning_to_old_value_and_ambiguity(world):
    fake, clock, run, tmp = world
    run.collect_corp_master("r1")
    for day, code in ((1, "000661"), (2, "000660")):            # A→B→A
        clock.t = T0 + timedelta(days=day)
        fake.corps = [(SAMSUNG, "삼성전자", "005930", "x"), (HYNIX, "SK하이닉스", code, "x")]
        assert run.collect_corp_master(f"r{day + 1}").counts["added"] == 1
    sm = master(tmp)
    assert [h.stock_code for h in sm.history(HYNIX)] == ["000660", "000661", "000660"]
    # 파일이 두 회사에 같은 종목코드를 주면 고르지 않는다
    clock.t = T0 + timedelta(days=3)
    fake.corps = [(SAMSUNG, "삼성전자", "000660", "x"), (HYNIX, "SK하이닉스", "000660", "x")]
    run.collect_corp_master("r4")
    r = master(tmp).entity_of("000660", CUT0 + timedelta(days=3))
    assert r.status is MapStatus.AMBIGUOUS and {m.entity_id for m in r.candidates} == {SAMSUNG, HYNIX}


def test_truncated_or_broken_file_changes_nothing(world, monkeypatch):
    fake, clock, run, tmp = world
    run.collect_corp_master("r1")
    before = master(tmp)
    clock.t = T0 + timedelta(days=1)
    monkeypatch.setattr(opendart_corpcode, "MIN_LISTED", 2)
    fake.corps = [(SAMSUNG, "삼성전자", "005930", "x")]            # 상장사가 너무 적음 = 잘린 파일
    m = run.collect_corp_master("r2")
    assert m.status == "failed" and "잘린" in m.errors[0]
    fake.corps = [(SAMSUNG, "삼성전자", "", "x"), (HYNIX, "SK하이닉스", "", "x"),
                  ("00999003", "채움1", "111110", "x"), ("00999004", "채움2", "111120", "x")]   # 한 번에 다 사라짐
    m = run.collect_corp_master("r3")
    assert m.status == "failed" and "한 번에 사라짐" in m.errors[0]
    fake.force_status = "020"                                   # 한도 초과 응답
    m = run.collect_corp_master("r4")
    assert m.status == "failed" and "020" in m.errors[0]
    after = master(tmp)
    assert [h.revision_id for h in after.history(HYNIX)] == [h.revision_id for h in before.history(HYNIX)]


def test_corp_code_zip_helper_matches_real_shape():
    rows = parse_corp_codes(__import__("zipfile").ZipFile(__import__("io").BytesIO(
        corp_code_zip([("00126380", "삼성전자", "005930", "20260301")]))).read("CORPCODE.xml"))
    assert rows == [{"corp_code": "00126380", "corp_name": "삼성전자", "stock_code": "005930", "modify_date": "20260301"}]
