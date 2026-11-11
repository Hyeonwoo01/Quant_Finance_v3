"""금융위원회 주식시세정보 수집기와 비공개 저장. 응답 형식은 공개 명세(기술문서)를 본떴고 값은 꾸민 숫자다."""
import json
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest

from collectors.base_client import CallBudget, IncompletePagination, TokenBucket
from collectors.fsc_krx import (SERIES_PRICE, FscClient, FscError, available_at, parse_page, price_records,
                                provider_available_at)
from core.data_contracts import PermissionDenied, Purpose
from core.pit_schema import PITStore
from core.source_registry import FSC_KRX_STOCK_PRICE, default_registry
from orchestration.krx_daily import KrxRun, check, last_published, probe, published_sessions, sessions_between
from storage.pit_store import PITFileStore, by_period_day
from storage.visibility import PRIVATE_MARKER

UTC = timezone.utc
KEY = "fsc+KEY/abc="
SAMSUNG, HYNIX = "KR7005930003", "KR7000660001"
NOW = datetime(2026, 11, 10, 5, 23, tzinfo=UTC)          # 11/10(화) 14:23 KST — 11/9(월) 자료가 나온 뒤


def row(isin, code, name, close, day="20261109", market="KOSPI", **kw):
    r = {"basDt": day, "srtnCd": code, "isinCd": isin, "itmsNm": name, "mrktCtg": market, "clpr": str(close),
         "vs": "100", "fltRt": ".5", "mkp": str(close - 100), "hipr": str(close + 200), "lopr": str(close - 300),
         "trqu": "1234567", "trPrc": "98765432100", "lstgStCnt": "5919637922", "mrktTotAmt": str(close * 5919637922)}
    r.update(kw)
    return r


def body(rows, total=None, page_no=1, code="00"):
    return json.dumps({"response": {"header": {"resultCode": code, "resultMsg": "NORMAL SERVICE."},
                                    "body": {"numOfRows": len(rows), "pageNo": page_no,
                                             "totalCount": len(rows) if total is None else total,
                                             "items": {"item": rows}}}}, ensure_ascii=False).encode()


class Fake:
    """기준일별 행을 돌려주는 가짜 게이트웨이. 인증·활용신청 문제는 실제처럼 HTTP 상태로 알린다."""

    def __init__(self):
        self.days = {"20261109": [row(SAMSUNG, "005930", "삼성전자", 71000), row(HYNIX, "000660", "SK하이닉스", 210000)],
                     "20261106": [row(SAMSUNG, "005930", "삼성전자", 70000, day="20261106")]}
        self.status, self.calls = 200, []

    def __call__(self, url, params, timeout):
        self.calls.append((urlparse(url).path, dict(params)))
        if self.status != 200:
            return self.status, {}, b"Forbidden"
        rows = self.days.get(params["basDt"], [])
        size, page = int(params["numOfRows"]), int(params["pageNo"])
        return 200, {}, body(rows[(page - 1) * size: page * size], total=len(rows), page_no=page)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def client(fake, clock, max_calls=50):
    return FscClient(KEY, budget=CallBudget(max_calls), rate=TokenBucket(1e9), transport=fake, sleep=lambda s: None,
                     now=clock, max_retries=0)


@pytest.fixture
def private_dir(tmp_path):
    (tmp_path / PRIVATE_MARKER).write_text("private\n")
    return tmp_path


# ---------- 계약과 시간 규칙 ----------

def test_contract_is_internal_only():
    reg = default_registry()
    cid = FSC_KRX_STOCK_PRICE.contract_id
    assert reg.is_allowed(cid, Purpose.STORAGE)
    for p in (Purpose.RAW_REDISTRIBUTION, Purpose.PUBLIC_DISPLAY, Purpose.ML_TRAINING, Purpose.DERIVED_USE, Purpose.LLM_INPUT):
        assert not reg.is_allowed(cid, p)                  # 한국거래소 답변 전까지 학습·파생 지표도 막는다


def test_availability_follows_provider_delay_even_for_backfill():
    """금요일(11/6) 종가는 월요일 13:00 KST에 온다 → 화요일 08:30 KST부터 쓸 수 있다. 몇 년 뒤에 받아도 같은 규칙."""
    fri = date(2026, 11, 6)
    assert provider_available_at(fri) == datetime(2026, 11, 9, 4, 0, tzinfo=UTC)
    assert available_at(fri, datetime(2026, 11, 9, 5, 0, tzinfo=UTC)) == datetime(2026, 11, 9, 23, 30, tzinfo=UTC)
    late = datetime(2026, 11, 20, 5, 0, tzinfo=UTC)        # 늦게 받으면 받은 뒤 첫 컷오프
    assert available_at(fri, late) > datetime(2026, 11, 20, 5, 0, tzinfo=UTC)
    # 지금 기준으로 공급자가 이미 준 마지막 기준일
    assert last_published(NOW) == date(2026, 11, 9)
    assert last_published(datetime(2026, 11, 10, 3, 0, tzinfo=UTC)) == date(2026, 11, 6)      # 13시 전
    assert last_published(datetime(2026, 11, 8, 6, 0, tzinfo=UTC)) == date(2026, 11, 5)       # 일요일
    assert published_sessions(NOW, 2) == [date(2026, 11, 6), date(2026, 11, 9)]
    assert sessions_between(date(2026, 11, 6), date(2026, 11, 10)) == [date(2026, 11, 6), date(2026, 11, 9), date(2026, 11, 10)]


# ---------- 응답 해석 ----------

def test_parse_page_and_gateway_errors():
    rows, total, page = parse_page(body([row(SAMSUNG, "005930", "삼성전자", 71000)], total=2500, page_no=3))
    assert (len(rows), total, page) == (1, 2500, 3)
    assert parse_page(json.dumps({"response": {"header": {"resultCode": "00"}, "body": {"totalCount": 0, "items": ""}}}).encode()) == ([], 0, 1)
    with pytest.raises(FscError):
        parse_page(body([], code="99"))
    with pytest.raises(FscError):
        parse_page(b"<html>error</html>")
    fake, clock = Fake(), Clock(NOW)
    fake.status = 403
    with pytest.raises(FscError) as e:
        client(fake, clock).prices(date(2026, 11, 9))
    assert e.value.not_applied and e.value.auth and "활용신청" in str(e.value)
    fake.status = 401
    with pytest.raises(FscError) as e:
        client(fake, clock).prices(date(2026, 11, 9))
    assert e.value.auth and not e.value.not_applied and KEY not in str(e.value)


def test_pagination_is_complete_or_fails():
    fake, clock = Fake(), Clock(NOW)
    fake.days["20261109"] = [row(f"KR7{i:06d}000", f"{i:06d}", f"종목{i}", 1000 + i) for i in range(1, 8)]
    pages = client(fake, clock).prices(date(2026, 11, 9), page_rows=3)
    assert [len(p.rows) for p in pages] == [3, 3, 1] and fake.calls[0][1]["serviceKey"] == KEY
    real = fake.__call__

    def short(url, params, timeout):                      # 둘째 페이지가 비어 온다
        return (200, {}, body([], total=7, page_no=2)) if params["pageNo"] == "2" else real(url, params, timeout)
    with pytest.raises(IncompletePagination):
        FscClient(KEY, budget=CallBudget(9), rate=TokenBucket(1e9), transport=short, sleep=lambda s: None).prices(
            date(2026, 11, 9), page_rows=3)


def test_price_records_values_revisions_and_bad_rows():
    store = PITStore(default_registry())
    day, t = date(2026, 11, 9), NOW
    rows = [row(SAMSUNG, "005930", "삼성전자", 71000), row(HYNIX, "000660", "SK하이닉스", 210000),
            row(SAMSUNG, "005930", "삼성전자", 71000),                       # 같은 종목 두 번
            row("KR7000000001", "000001", "이상한행", 1, clpr="N/A"),         # 숫자가 아님
            row("KR7000000002", "000002", "다른날", 1, day="20261106")]       # 기준일이 다름
    recs, counts = price_records(rows, day, contract_id="fsc_krx_stock_price_v1", raw_sha="h", ingested_at_utc=t,
                                 latest=store.latest)
    assert (len(recs), counts["bad_rows"], counts["duplicates"]) == (2, 2, 1)
    s = recs[0]
    assert s.entity_id == SAMSUNG and json.loads(s.value) == {
        "open": 70900, "high": 71200, "low": 70700, "close": 71000, "volume": 1234567, "value": 98765432100,
        "shares": 5919637922, "mktcap": 71000 * 5919637922}
    assert json.loads(s.attributes) == {"srtnCd": "005930", "name": "삼성전자", "market": "KOSPI"}
    assert s.event_time_utc == datetime(2026, 11, 9, 6, 30, tzinfo=UTC)           # 15:30 KST 종가
    assert s.provider_available_at_utc == datetime(2026, 11, 10, 4, 0, tzinfo=UTC)
    assert s.available_at_utc == datetime(2026, 11, 10, 23, 30, tzinfo=UTC)       # 11/11 08:30 KST
    for r in recs:
        store.append(r)
    again, c2 = price_records(rows[:2], day, contract_id="fsc_krx_stock_price_v1", raw_sha="h2",
                              ingested_at_utc=t + timedelta(days=1), latest=store.latest)
    assert again == [] and c2["unchanged"] == 2
    fixed, c3 = price_records([row(SAMSUNG, "005930", "삼성전자", 71500)], day, contract_id="fsc_krx_stock_price_v1",
                              raw_sha="h3", ingested_at_utc=t + timedelta(days=2), latest=store.latest)
    assert c3["revised"] == 1 and fixed[0].supersedes_revision_id == s.revision_id


# ---------- 전체 실행 ----------

def test_run_stores_only_in_private_store_and_is_idempotent(private_dir):
    fake, clock = Fake(), Clock(NOW)
    days = [date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 9)]
    m = KrxRun(client(fake, clock), private_dir, now=clock, version="test").run(days)
    assert m.status == "ok" and m.counts["added"] == 3 and m.counts["days"] == 2 and m.counts["not_yet"] == 1
    assert "2026-11-05: 아직 자료 없음" in m.warnings[0]
    files = sorted(p.name for p in (private_dir / "pit_versions" / SERIES_PRICE).iterdir())
    assert files == ["2026-11-06.jsonl", "2026-11-09.jsonl"]
    for p in private_dir.rglob("*"):                                           # 인증키는 어디에도 남지 않는다
        if p.is_file():
            data = p.read_bytes()
            assert KEY.encode() not in data and b"fsc%2BKEY" not in data
    clock.t = NOW + timedelta(days=1)
    m2 = KrxRun(client(fake, clock), private_dir, now=clock, version="test").run(days)
    assert m2.counts.get("added", 0) == 0 and m2.counts["unchanged"] == 3
    # 과거 채우기는 이미 받은 날을 다시 부르지 않는다
    n = len(fake.calls)
    m3 = KrxRun(client(fake, clock), private_dir, now=clock, version="test").run(days, skip_existing=True)
    assert m3.counts["skipped_existing"] == 2 and len(fake.calls) == n + 1
    # as-of: 11/10 14:23 KST에 받았으므로 둘 다 11/11 08:30 KST 전에는 보이지 않는다 (받은 뒤 첫 컷오프)
    store = PITFileStore(private_dir, default_registry(), partitions={SERIES_PRICE: by_period_day}).load([SERIES_PRICE])
    assert store.query_as_of(datetime(2026, 11, 10, 23, 29, tzinfo=UTC), Purpose.STORAGE).empty
    assert len(store.query_as_of(datetime(2026, 11, 10, 23, 30, tzinfo=UTC), Purpose.STORAGE)) == 3
    assert store.query_as_of(datetime(2026, 11, 12, tzinfo=UTC), Purpose.ML_TRAINING).empty   # 학습 목적으로는 안 나온다


def test_public_store_gets_nothing_not_even_a_call(tmp_path):
    """표식이 없는 저장소(공개 data 브랜치)를 가리키면 한 번도 호출하지 않고, 파일도 수집 기록도 남기지 않는다."""
    fake, clock = Fake(), Clock(NOW)
    m = KrxRun(client(fake, clock), tmp_path, now=clock, version="test").run([date(2026, 11, 9)])
    assert m.status == "failed" and PRIVATE_MARKER in m.errors[0]
    assert fake.calls == [] and list(tmp_path.iterdir()) == []
    # 표식 확인을 건너뛰더라도 저장소가 직접 막는다
    run = KrxRun(client(fake, clock), tmp_path, now=clock, version="test")
    with pytest.raises(PermissionDenied, match="공개 저장소"):
        run.collect_day(date(2026, 11, 9), m)
    assert not (tmp_path / "raw").exists() and not (tmp_path / "pit_versions").exists()


def test_not_applied_stops_run_and_check_probe_print_no_prices(private_dir):
    fake, clock = Fake(), Clock(NOW)
    fake.status = 403
    m = KrxRun(client(fake, clock), private_dir, now=clock, version="test").run([date(2026, 11, 6), date(2026, 11, 9)])
    assert m.status == "failed" and len(fake.calls) == 1 and "활용신청" in m.errors[0]
    fake.status = 200
    text = check(client(fake, clock), date(2026, 11, 9)) + "\n" + "\n".join(probe(client(fake, clock), date(2026, 11, 9)))
    assert "2건" in text and "KOSPI" in text and "clpr" in text
    for secret in ("71000", "210000", "98765432100", "5919637922", KEY):          # 시세 값·인증키는 로그에 없다
        assert secret not in text


def test_redo_rewrites_named_days_only_in_private_store(private_dir, tmp_path_factory):
    """잘못된 규칙으로 저장된 날을 지우고 다시 받는다. 다른 날은 건드리지 않고, 이유를 수집 기록에 남긴다."""
    fake, clock = Fake(), Clock(NOW)
    run = KrxRun(client(fake, clock), private_dir, now=clock, version="test")
    run.run([date(2026, 11, 6), date(2026, 11, 9)])
    f06 = private_dir / "pit_versions" / SERIES_PRICE / "2026-11-06.jsonl"
    f09 = private_dir / "pit_versions" / SERIES_PRICE / "2026-11-09.jsonl"
    before09 = f09.read_bytes()
    clock.t = NOW + timedelta(days=1)
    m = KrxRun(client(fake, clock), private_dir, now=clock, version="test").redo([date(2026, 11, 6)], "캘린더 수정")
    assert m.counts["added"] == 1 and m.target["removed_days"] == ["2026-11-06"] and "캘린더 수정" in m.warnings[0]
    assert f09.read_bytes() == before09                                       # 다른 날은 그대로
    recs = [json.loads(l) for l in f06.read_text().splitlines()]
    assert len(recs) == 1 and recs[0]["ingested_at_utc"].startswith("2026-11-11")
    saved = json.loads(next((private_dir / "manifests").rglob(f"{m.run_id}.json")).read_text())
    assert saved["target"]["redo_reason"] == "캘린더 수정"
    public = tmp_path_factory.mktemp("public")                               # 공개 저장소에는 아무것도 쓰지 않는다
    m2 = KrxRun(client(fake, clock), public, now=clock, version="test").redo([date(2026, 11, 6)], "x")
    assert m2.status == "failed" and list(public.iterdir()) == []
