"""OpenDART 수집기: 보고서명 해석, 가용 시각, PIT 변환, 클라이언트 오류 처리."""
import json
from datetime import date, datetime, timezone

import pytest

from collectors.base_client import CallBudget, TokenBucket
from collectors.opendart import (FilingRef, OpenDartClient, OpenDartError, available_at, filing_record, fs_candidates,
                                 fs_records, parse_amount, parse_periodic)
from core.pit_schema import PITStore
from tests.fake_opendart import TEST_KEY, FakeDart

UTC = timezone.utc


def client(fake, max_calls=100):
    return OpenDartClient(TEST_KEY, budget=CallBudget(max_calls), rate=TokenBucket(1e9, sleep=lambda s: None),
                          transport=fake, sleep=lambda s: None)


# ---------- 보고서명 ----------

@pytest.mark.parametrize("name, kind, end, tags, changes", [
    ("분기보고서 (2026.09)", "분기보고서", date(2026, 9, 30), (), True),
    ("[기재정정]사업보고서 (2025.12)", "사업보고서", date(2025, 12, 31), ("기재정정",), True),
    ("[첨부정정]반기보고서 (2026.06)", "반기보고서", date(2026, 6, 30), ("첨부정정",), False),
    ("[기재정정][첨부추가]분기보고서 (2026.03)", "분기보고서", date(2026, 3, 31), ("기재정정", "첨부추가"), True),
])
def test_parse_periodic(name, kind, end, tags, changes):
    rep = parse_periodic(name)
    assert (rep.kind, rep.period_end, rep.tags, rep.changes_financials) == (kind, end, tags, changes)


@pytest.mark.parametrize("name", ["주요사항보고서(자기주식취득결정)", "임원ㆍ주요주주특정증권등소유상황보고서", "분기보고서 (2026.13)", ""])
def test_non_periodic_names(name):
    assert parse_periodic(name) is None


def test_fs_candidates_try_december_fiscal_year_first():
    assert fs_candidates(parse_periodic("분기보고서 (2026.09)"))[0] == (2026, "11014")
    assert fs_candidates(parse_periodic("분기보고서 (2026.03)"))[0] == (2026, "11013")
    assert fs_candidates(parse_periodic("반기보고서 (2026.06)")) == [(2026, "11012"), (2025, "11012")]
    assert fs_candidates(parse_periodic("사업보고서 (2015.12)")) == [(2015, "11011")]     # 2014년 이전은 제공 안 함


def test_parse_amount():
    assert parse_amount("9,999,999") == 9999999 and parse_amount("-1,234") == -1234
    assert parse_amount("") is None and parse_amount(None) is None and parse_amount("-") is None
    with pytest.raises(ValueError):
        parse_amount("abc")


# ---------- 가용 시각 ----------

def test_available_at_is_rule_or_actual_ingestion_whichever_later():
    mon_0830 = datetime(2026, 11, 8, 23, 30, tzinfo=UTC)     # 2026-11-09(월) 08:30 KST
    # 금요일 접수, 월요일 06:17 KST에 수집 → 월요일 08:30
    assert available_at(date(2026, 11, 6), datetime(2026, 11, 8, 21, 17, tzinfo=UTC)) == mon_0830
    # 같은 공시를 월요일 10:00 KST에야 받았다면 → 화요일 08:30 (받기 전 시점에 쓰지 않는다)
    assert available_at(date(2026, 11, 6), datetime(2026, 11, 9, 1, 0, tzinfo=UTC)) == datetime(2026, 11, 9, 23, 30, tzinfo=UTC)


# ---------- PIT 변환 ----------

def test_filing_revision_changes_only_when_content_changes():
    fake = FakeDart()
    row = fake.filing("20261106000123", "00126380", "분기보고서 (2026.09)", rm="유")
    t = datetime(2026, 11, 8, 21, 17, tzinfo=UTC)
    a = filing_record(row, contract_id="c", raw_sha="h", ingested_at_utc=t)
    b = filing_record(dict(row), contract_id="c", raw_sha="h2", ingested_at_utc=t)
    c = filing_record({**row, "rm": "유정"}, contract_id="c", raw_sha="h3", ingested_at_utc=t)
    assert a.revision_id == b.revision_id != c.revision_id and a.observation_key == c.observation_key
    assert json.loads(c.value)["rm"] == "유정" and a.source_url.endswith("20261106000123")
    assert a.vintage_date == a.observation_period_start == date(2026, 11, 6)


def _fs_rows(rcept_no, revenue, op):
    fake = FakeDart()
    fake.statement(rcept_no, "00126380", 2026, "11014", "OFS",
                   [("IS", "ifrs-full_Revenue", "매출액", revenue, "300,000"),
                    ("IS", "dart_OperatingIncomeLoss", "영업이익", op, None),
                    ("BS", "ifrs-full_Assets", "자산총계", "1,000,000", None),
                    ("SCE", "ifrs-full_Equity", "자본", "1", None)])           # 자본변동표는 받지 않음
    return fake.fs[("00126380", "2026", "11014", "OFS")]


def test_fs_records_and_correction_supersedes_only_changed_accounts(registry):
    store = PITStore(registry)
    t1, t2 = datetime(2026, 11, 14, 21, 0, tzinfo=UTC), datetime(2026, 11, 20, 21, 0, tzinfo=UTC)
    orig = FilingRef("20261114000500", "00126380", date(2026, 11, 14), date(2026, 9, 30))
    conv = fs_records(_fs_rows(orig.rcept_no, "100,000", "10,000"), filing=orig, fs_div="OFS", contract_id="kr_price",
                      raw_sha="h", ingested_at_utc=t1, latest=store.latest)
    metrics = {r.metric_id: r.value for r in conv.records}
    assert metrics == {"OFS|IS|ifrs-full_Revenue|매출액|당기": 100000, "OFS|IS|ifrs-full_Revenue|매출액|당기누적": 300000,
                       "OFS|IS|dart_OperatingIncomeLoss|영업이익|당기": 10000, "OFS|BS|ifrs-full_Assets|자산총계|당기": 1000000}
    for r in conv.records:
        store.append(r)

    fix = FilingRef("20261120000700", "00126380", date(2026, 11, 20), date(2026, 9, 30))
    conv2 = fs_records(_fs_rows(fix.rcept_no, "100,000", "9,000"), filing=fix, fs_div="OFS", contract_id="kr_price",
                       raw_sha="h2", ingested_at_utc=t2, latest=store.latest)
    assert [(r.metric_id, r.value) for r in conv2.records] == [("OFS|IS|dart_OperatingIncomeLoss|영업이익|당기", 9000)]
    assert conv2.records[0].supersedes_revision_id == f"{orig.rcept_no}|OFS|IS|dart_OperatingIncomeLoss|영업이익|당기"
    assert conv2.unchanged_by_correction == 3
    store.append(conv2.records[0])

    # 원본이 정정본보다 늦게 처리되면 넣지 않는다
    late = fs_records(_fs_rows(orig.rcept_no, "1", "1"), filing=orig, fs_div="OFS", contract_id="kr_price",
                      raw_sha="h3", ingested_at_utc=t2, latest=store.latest)
    assert late.older_than_stored == 1


def test_fs_records_reports_unparseable_amounts():
    rows = _fs_rows("20261114000500", "N/A", "10")
    conv = fs_records(rows, filing=FilingRef("20261114000500", "00126380", date(2026, 11, 14), date(2026, 9, 30)),
                      fs_div="OFS", contract_id="c", raw_sha="h", ingested_at_utc=datetime(2026, 11, 14, 21, tzinfo=UTC),
                      latest=lambda k: None)
    assert len(conv.parse_errors) == 1 and "N/A" in conv.parse_errors[0]
    assert len(conv.records) == 3


# ---------- 클라이언트 ----------

def test_list_pagination_and_status_handling():
    fake = FakeDart()
    for i in range(150):
        fake.filing(f"20261106{i:06d}", f"{i:08d}", "주요사항보고서(자기주식취득결정)")
    c = client(fake)
    pages, problem = c.list_filings(date(2026, 11, 6), date(2026, 11, 6), "Y")
    assert problem is None and [len(p.rows) for p in pages] == [100, 50]
    assert all(call[1]["page_count"] == "100" and call[1]["sort_mth"] == "asc" for call in fake.calls)

    pages, problem = c.list_filings(date(2026, 11, 7), date(2026, 11, 8), "Y")      # 주말: 013
    assert problem is None and pages[0].status == "013" and pages[0].rows == []
    with pytest.raises(ValueError, match="3개월"):
        c.list_filings(date(2026, 1, 1), date(2026, 6, 1), "Y")


def test_fatal_status_stops_and_key_never_appears():
    fake = FakeDart()
    fake.force_status = "020"
    with pytest.raises(OpenDartError) as e:
        client(fake).list_filings(date(2026, 11, 6), date(2026, 11, 6), "Y")
    assert e.value.stop_all and TEST_KEY not in str(e.value)
    bad = OpenDartClient("WRONG", budget=CallBudget(5), rate=TokenBucket(1e9), transport=fake, sleep=lambda s: None)
    fake.force_status = None
    with pytest.raises(OpenDartError) as e:
        bad.call("list.json", {})
    assert e.value.status == "010" and e.value.stop_all and "WRONG" not in str(e.value)
    with pytest.raises(ValueError, match="DART_API_KEY"):
        OpenDartClient("", budget=CallBudget(1))


def test_financial_statement_validates_request():
    c = client(FakeDart())
    with pytest.raises(ValueError):
        c.financial_statement("00126380", 2014, "11011", "OFS")
    with pytest.raises(ValueError):
        c.financial_statement("00126380", 2026, "99999", "OFS")
