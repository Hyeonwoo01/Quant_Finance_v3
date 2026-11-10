"""금융위원회 주식시세정보(원천: 한국거래소) 수집 — 일별 시가·고가·저가·종가·거래량·거래대금·상장주식수·시가총액.

계약: fsc_krx_stock_price_v1 (DATA_SOURCES.md '한국 주식 가격'). 재배포가 금지된 데이터라 비공개 저장소에만 둔다.
공개 저장소의 Actions 로그도 공개이므로, 이 모듈과 실행 코드는 시세 값을 출력하지 않는다 (건수·필드 이름만).

시간 규칙 (가이드 4-1, 4-4):
- 공급자는 기준일(T)의 값을 "영업일 하루 뒤 오후 1시 이후"에 준다. 그래서 공급자 가용 시각은 T 다음 KRX 세션의
  13:00 KST이고, 가용 시각은 그 뒤 첫 08:30 KST 컷오프(= T+2 세션 장 시작 전)와 실제로 받은 시각 이후 첫 컷오프
  중 늦은 쪽이다. 과거 날짜를 한꺼번에 받을 때도 같은 규칙을 쓴다 — 백테스트가 이 공급원으로 실전에서 얻을 수
  있는 것보다 빠른 정보를 쓰지 않게 하기 위해서다. 실제 종가 시각은 event_time_utc에 따로 남긴다.
- 종목 식별자는 ISIN이다. 단축코드는 다른 종목이 다시 쓸 수 있어 부가 정보로만 둔다.
"""
import hashlib
import json
import urllib.parse
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from typing import Callable, Optional

import pandas as pd

from collectors.base_client import (BaseClient, CallBudget, CollectorError, HttpResponse, RequestFailed, TokenBucket,
                                    check_pagination, check_schema)
from core.calendar_align import KR_VENUE, KST, calendar, first_kr_cutoff_at_or_after, next_session_after, session_close_utc
from core.pit_schema import PITRecord

SERIES_PRICE = "krx_daily_price"
METRIC_OHLCV = "ohlcv"
PROVIDER = "FSC"
SOURCE_URL = "https://www.data.go.kr/data/15094808/openapi.do"
BASE = "https://apis.data.go.kr/1160100"
PRICE_PATH = "GetStockSecuritiesInfoService_V2/getStockPriceInfo_V2"
PAGE_ROWS = 5000                                   # 한 번에 최대 10,000행까지 준다
PROVIDER_READY = time(13, 0)                       # 기준일 다음 영업일 13:00 KST 이후 갱신

# 응답 필드 → 저장 이름. 모두 정수(원·주)다
VALUE_FIELDS = {"mkp": "open", "hipr": "high", "lopr": "low", "clpr": "close", "trqu": "volume", "trPrc": "value",
                "lstgStCnt": "shares", "mrktTotAmt": "mktcap"}
PRICE_REQUIRED = ("basDt", "srtnCd", "isinCd", "itmsNm", "mrktCtg", *VALUE_FIELDS)
PRICE_KNOWN = ("vs", "fltRt")


class FscError(CollectorError):
    def __init__(self, code: str, message: str, *, auth: bool = False, not_applied: bool = False):
        super().__init__(f"FSC: {code} {message}".strip())
        self.code, self.message, self.auth, self.not_applied = code, message, auth, not_applied


@dataclass(frozen=True)
class Page:
    response: HttpResponse
    rows: list
    total: int
    page_no: int


def parse_page(body: bytes) -> tuple[list, int, int]:
    """JSON 응답 → (행, 전체 건수, 페이지 번호). 정상 코드가 아니면 FscError."""
    try:
        d = json.loads(body.decode("utf-8"))
        resp = d["response"]
        header = resp["header"]
    except (ValueError, UnicodeDecodeError, KeyError, TypeError):
        raise FscError("parse", "예상한 JSON 구조가 아님") from None
    code = str(header.get("resultCode", "?"))
    if code not in ("00", "0"):
        raise FscError(code, str(header.get("resultMsg", "")))
    body_ = resp.get("body") or {}
    items = body_.get("items") or {}
    rows = items.get("item", []) if isinstance(items, dict) else []
    if isinstance(rows, dict):                     # 한 건일 때 목록이 아니라 객체로 오는 경우
        rows = [rows]
    return rows, int(body_.get("totalCount") or 0), int(body_.get("pageNo") or 1)


class FscClient:
    def __init__(self, key: str, *, budget: CallBudget, rate: Optional[TokenBucket] = None, **http_kw):
        if not key:
            raise ValueError("공공데이터포털 인증키(DATA_GO_KR_KEY)가 없음")
        self._key = urllib.parse.unquote(key)      # 인코딩 키·디코딩 키 어느 쪽이든 한 번만 인코딩되게
        self.budget = budget
        self.http = BaseClient(BASE, secret_params=("serviceKey",), secret_values=(key, self._key,
                               urllib.parse.quote(self._key, safe="")), budget=budget,
                               rate=rate if rate is not None else TokenBucket(5.0), **http_kw)

    def _get(self, path: str, params: dict) -> Page:
        try:
            r = self.http.get(path, {"serviceKey": self._key, "resultType": "json", **params})
        except RequestFailed as e:                 # 게이트웨이는 인증·신청 문제를 HTTP 상태로 알린다
            if e.status == 401:
                raise FscError("401", "인증키가 없거나 유효하지 않음", auth=True) from None
            if e.status == 403:
                raise FscError("403", "이 API의 활용신청 내역이 없음 (공공데이터포털에서 활용신청 필요)",
                               auth=True, not_applied=True) from None
            raise
        rows, total, page_no = parse_page(r.body)
        return Page(r, rows, total, page_no)

    def prices(self, bas_dt: date, page_rows: int = PAGE_ROWS) -> list[Page]:
        """기준일 하루의 모든 종목 시세 (모든 페이지). 건수가 총계와 다르면 IncompletePagination."""
        pages, page_no = [], 1
        while True:
            p = self._get(PRICE_PATH, {"basDt": f"{bas_dt:%Y%m%d}", "numOfRows": page_rows, "pageNo": page_no})
            pages.append(p)
            if page_no * page_rows >= p.total or not p.rows:
                break
            page_no += 1
        total = pages[0].total
        check_pagination(expected_rows=total, got_rows=sum(len(p.rows) for p in pages),
                         expected_pages=max(1, -(-total // page_rows)), got_pages=len(pages))
        return pages


def provider_available_at(bas_dt: date) -> datetime:
    """기준일 다음 KRX 세션의 13:00 KST (UTC)."""
    nxt = next_session_after(bas_dt)
    return pd.Timestamp.combine(nxt.date(), PROVIDER_READY).tz_localize(KST).tz_convert("UTC").to_pydatetime()


def available_at(bas_dt: date, ingested_at_utc: datetime) -> datetime:
    rule = first_kr_cutoff_at_or_after(provider_available_at(bas_dt))
    return max(rule, first_kr_cutoff_at_or_after(ingested_at_utc)).to_pydatetime()


def _int(s) -> Optional[int]:
    if s is None:
        return None
    t = str(s).replace(",", "").strip()
    if t in ("", "-"):
        return None
    return int(t)


def price_records(rows: list, bas_dt: date, *, contract_id: str, raw_sha: str, ingested_at_utc: datetime,
                  latest: Callable[[tuple], Optional[PITRecord]]) -> tuple[list, dict]:
    """하루치 시세 행 → 종목마다 레코드 하나 (값은 {open, high, low, close, volume, value, shares, mktcap} JSON).

    반환: (새 레코드, 건수). 다시 받은 같은 값은 넣지 않고, 값이 바뀌었으면 이전 버전을 가리킨다.
    기준일이 다르거나 숫자가 아닌 행은 건너뛰고 건수(bad_rows)로 남긴다.
    """
    cal = calendar(KR_VENUE)
    day = pd.Timestamp(bas_dt)
    close = session_close_utc(day, KR_VENUE).to_pydatetime() if cal.is_session(day) else None
    prov, avail = provider_available_at(bas_dt), available_at(bas_dt, ingested_at_utc)
    counts = {"rows": len(rows), "bad_rows": 0, "duplicates": 0, "unchanged": 0, "revised": 0}
    out, seen = [], set()
    for row in rows:
        try:
            isin = row["isinCd"].strip()
            if row["basDt"].strip() != f"{bas_dt:%Y%m%d}" or not isin:
                raise ValueError("기준일·ISIN 이상")
            content = {name: _int(row[f]) for f, name in VALUE_FIELDS.items()}
        except (KeyError, ValueError, AttributeError):
            counts["bad_rows"] += 1
            continue
        if isin in seen:
            counts["duplicates"] += 1
            continue
        seen.add(isin)
        value = json.dumps(content, sort_keys=True)
        attrs = {"srtnCd": row["srtnCd"].strip(), "name": row["itmsNm"].strip(), "market": row["mrktCtg"].strip()}
        rec = PITRecord(entity_id=isin, series_id=SERIES_PRICE, metric_id=METRIC_OHLCV, available_at_utc=avail,
                        ingested_at_utc=ingested_at_utc,
                        revision_id=f"{isin}|{bas_dt:%Y%m%d}|{hashlib.sha256(value.encode()).hexdigest()[:10]}",
                        value=value, unit="json", source_provider=PROVIDER, raw_payload_hash=raw_sha,
                        license_contract_id=contract_id, observation_period_start=bas_dt, observation_period_end=bas_dt,
                        event_time_utc=close, provider_available_at_utc=prov, currency="KRW", source_url=SOURCE_URL,
                        attributes=json.dumps(attrs, ensure_ascii=False, sort_keys=True))
        prev = latest(rec.observation_key)
        if prev is not None:
            if prev.value == rec.value:
                counts["unchanged"] += 1
                continue
            counts["revised"] += 1
            rec = replace(rec, revision_id=f"{rec.revision_id}@{ingested_at_utc:%Y%m%dT%H%M%S}",
                          supersedes_revision_id=prev.revision_id)
        out.append(rec)
    return out, counts


def schema_problems(rows: list) -> tuple[tuple, tuple]:
    """(빠진 필수 필드, 처음 보는 필드)."""
    s = check_schema(rows, PRICE_REQUIRED, PRICE_KNOWN)
    return s.missing, s.unexpected
