"""OpenDART 공시 목록·재무제표 수집과 PIT 레코드 변환 (가이드 3-3, 4-3).

API 명세는 OpenDART 개발가이드 원문으로 확인했다 (2026-11-09).
- 공시검색 list.json: 고유번호 없이 검색하면 기간 3개월 이내, page_count 최대 100. rcept_dt는 날짜(YYYYMMDD)뿐이고
  시각이 없다. rm(비고)은 나중에 바뀔 수 있다 (예: '정' 정정신고 있음, '철' 철회).
- 단일회사 전체 재무제표 fnlttSinglAcntAll.json: corp_code·bsns_year(2015년~)·reprt_code·fs_div(OFS/CFS) 필수.
  분·반기 손익계산서의 thstrm_amount는 3개월 금액, thstrm_add_amount는 누적 금액.
- 오류는 HTTP 200 안의 status로 온다: 000 정상, 013 데이터 없음, 020 요청 제한 초과(일반적으로 하루 20,000건) 등.

가용 시각: 접수일 다음 KRX 세션 08:30 KST (4-3)와, 우리가 실제로 받은 시각 이후 첫 08:30 컷오프 중 늦은 쪽.
나중에 받은 기록이 과거 시점에 쓰이지 않게 하기 위해서다 (늦게 채운 기록으로 과거를 고치지 않는다).
"""
import calendar as _calendar
import hashlib
import json
import re
import time
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Callable, Optional

from collectors.base_client import BaseClient, CallBudget, CollectorError, HttpResponse, TokenBucket, check_pagination
from core.calendar_align import dart_available_at, first_kr_cutoff_at_or_after
from core.pit_schema import PITRecord

BASE_URL = "https://opendart.fss.or.kr/api"
DAILY_REQUEST_LIMIT = 20_000           # 개발가이드 오류 코드 020 설명. 같은 키를 쓰는 다른 작업과 나눠 쓴다
OK, NO_DATA = "000", "013"
# 더 호출해도 소용없거나 해가 되는 상태: 키 문제(010·011·012·901), 한도 초과(020), 시스템 점검(800)
STOP_ALL = frozenset({"010", "011", "012", "020", "800", "901"})
PROVIDER = "OpenDART"
VIEWER_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={}"

LIST_REQUIRED = ("corp_cls", "corp_name", "corp_code", "stock_code", "report_nm", "rcept_no", "flr_nm", "rcept_dt", "rm")
FS_REQUIRED = ("rcept_no", "reprt_code", "bsns_year", "corp_code", "sj_div", "account_id", "account_nm", "thstrm_amount")
FS_KNOWN = ("sj_nm", "account_detail", "thstrm_nm", "thstrm_add_amount", "frmtrm_nm", "frmtrm_amount", "frmtrm_q_nm",
            "frmtrm_q_amount", "frmtrm_add_amount", "bfefrmtrm_nm", "bfefrmtrm_amount", "ord", "currency")
FS_STATEMENTS = ("BS", "IS", "CIS", "CF")   # SCE(자본변동표)는 계정 상세(account_detail) 구조가 달라 아직 받지 않는다
REPORT_CODES = {"11013": "1분기", "11012": "반기", "11014": "3분기", "11011": "사업"}

SERIES_FILINGS, SERIES_FS = "dart_filings", "dart_fs"


class OpenDartError(CollectorError):
    def __init__(self, status: str, message: str, endpoint: str):
        super().__init__(f"OpenDART {endpoint}: status {status} {message}".strip())
        self.status, self.message, self.endpoint = status, message, endpoint

    @property
    def stop_all(self) -> bool:
        return self.status in STOP_ALL


@dataclass(frozen=True)
class ApiResult:
    endpoint: str
    response: HttpResponse
    data: dict

    @property
    def status(self) -> str:
        return str(self.data.get("status"))

    @property
    def rows(self) -> list:
        return self.data.get("list") or []


class OpenDartClient:
    def __init__(self, api_key: str, *, budget: CallBudget, rate: Optional[TokenBucket] = None, **http_kw):
        if not api_key:
            raise ValueError("OpenDART 인증키(DART_API_KEY)가 없음")
        self._key = api_key
        self.budget = budget
        self.http = BaseClient(BASE_URL, secret_params=("crtfc_key",), budget=budget,
                               rate=rate if rate is not None else TokenBucket(3.0), **http_kw)

    def call(self, endpoint: str, params: dict) -> ApiResult:
        resp = self.http.get(endpoint, {"crtfc_key": self._key, **params})
        try:
            data = resp.json()
        except (ValueError, UnicodeDecodeError):
            raise OpenDartError("parse", "JSON이 아닌 응답", endpoint) from None
        result = ApiResult(endpoint, resp, data)
        if result.status not in (OK, NO_DATA):
            raise OpenDartError(result.status, str(data.get("message", "")), endpoint)
        return result

    def list_filings(self, bgn: date, end: date, corp_cls: str) -> tuple[list[ApiResult], Optional[str]]:
        """접수일 bgn~end(포함) 공시의 모든 페이지. 반환: (페이지들, 완결성 문제 설명 또는 None).

        끝난 날짜만 묻는다 (오늘은 아직 공시가 들어오는 중이라 페이지가 밀릴 수 있다). 완결성이 깨져도 받은 페이지는
        돌려준다 — 다음 실행이 같은 기간을 다시 훑는다.
        """
        if bgn > end or (end - bgn).days > 89:
            raise ValueError("고유번호 없는 공시검색은 3개월 이내, bgn <= end")
        pages, page = [], 1
        while True:
            r = self.call("list.json", {"bgn_de": f"{bgn:%Y%m%d}", "end_de": f"{end:%Y%m%d}", "corp_cls": corp_cls,
                                        "page_no": page, "page_count": 100, "sort": "date", "sort_mth": "asc"})
            pages.append(r)
            if r.status == NO_DATA:
                return pages, None
            total_page = int(r.data.get("total_page") or 1)
            if page >= total_page:
                break
            page += 1
        try:
            check_pagination(expected_rows=int(pages[0].data.get("total_count") or 0),
                             got_rows=sum(len(p.rows) for p in pages),
                             expected_pages=int(pages[0].data.get("total_page") or 1), got_pages=len(pages))
        except CollectorError as e:
            return pages, f"list.json {corp_cls} {bgn}~{end}: {e}"
        return pages, None

    def financial_statement(self, corp_code: str, bsns_year: int, reprt_code: str, fs_div: str) -> ApiResult:
        if reprt_code not in REPORT_CODES or fs_div not in ("OFS", "CFS") or bsns_year < 2015:
            raise ValueError(f"잘못된 재무제표 요청: {bsns_year} {reprt_code} {fs_div}")
        return self.call("fnlttSinglAcntAll.json", {"corp_code": corp_code, "bsns_year": str(bsns_year),
                                                    "reprt_code": reprt_code, "fs_div": fs_div})


# ---------------- 보고서명 해석 ----------------

_TAG = re.compile(r"^\s*\[([^\]]+)\]\s*")
_PERIODIC = re.compile(r"^(사업보고서|반기보고서|분기보고서)\s*\((\d{4})\.(\d{2})\)")


@dataclass(frozen=True)
class PeriodicReport:
    kind: str            # 사업보고서 · 반기보고서 · 분기보고서
    period_end: date     # 보고서명 괄호 안 연·월의 마지막 날
    tags: tuple          # 보고서명 앞의 [기재정정]·[첨부정정] 같은 꼬리표

    @property
    def changes_financials(self) -> bool:
        """재무 숫자가 새로 나오거나 바뀔 수 있는 보고서: 최초 제출 또는 [기재정정]. 첨부만 바뀐 것은 아니다."""
        return not self.tags or "기재정정" in self.tags


def parse_periodic(report_nm: str) -> Optional[PeriodicReport]:
    name, tags = (report_nm or "").strip(), []
    while (m := _TAG.match(name)):
        tags.append(m.group(1).strip())
        name = name[m.end():]
    m = _PERIODIC.match(name)
    if not m:
        return None
    y, mo = int(m.group(2)), int(m.group(3))
    if not 1 <= mo <= 12:
        return None
    return PeriodicReport(m.group(1), date(y, mo, _calendar.monthrange(y, mo)[1]), tuple(tags))


def fs_candidates(rep: PeriodicReport) -> list[tuple[int, str]]:
    """(사업연도, 보고서 코드) 후보. 12월 결산 회사의 규칙(괄호 안 연도, 3월=1분기·9월=3분기)을 먼저 두고,
    다른 결산월 회사를 위한 후보를 뒤에 둔다. 실제로 맞는지는 응답의 접수번호로 확인한다."""
    y, mo = rep.period_end.year, rep.period_end.month
    if rep.kind == "사업보고서":
        codes = ["11011"]
    elif rep.kind == "반기보고서":
        codes = ["11012"]
    else:
        codes = ["11013", "11014"] if mo <= 6 else ["11014", "11013"]
    return [(yy, c) for yy in (y, y - 1) for c in codes if yy >= 2015]


# ---------------- PIT 레코드 변환 ----------------

def parse_rcept_dt(s: str) -> date:
    return datetime.strptime(s.strip(), "%Y%m%d").date()


def available_at(rcept_dt: date, ingested_at_utc: datetime) -> datetime:
    """접수일 규칙(다음 KRX 세션 08:30 KST)과 실제 수집 시각 이후 첫 컷오프 중 늦은 쪽 (UTC)."""
    return max(dart_available_at(rcept_dt), first_kr_cutoff_at_or_after(ingested_at_utc)).to_pydatetime()


def parse_amount(s) -> Optional[int]:
    """'9,999,999' → 9999999, '-1,234' → -1234, 빈 값 → None. 숫자가 아니면 ValueError."""
    if s is None:
        return None
    t = str(s).replace(",", "").strip()
    if t in ("", "-"):
        return None
    return int(t)


def filing_record(row: dict, *, contract_id: str, raw_sha: str, ingested_at_utc: datetime) -> PITRecord:
    """공시 한 건 = 관측값 하나 (접수번호). 내용(비고 등)이 바뀌면 revision_id가 달라져 새 버전이 된다."""
    rcept_dt = parse_rcept_dt(row["rcept_dt"])
    content = {k: (row.get(k) or "").strip() for k in ("corp_cls", "corp_name", "stock_code", "report_nm", "flr_nm", "rm")}
    value = json.dumps(content, ensure_ascii=False, sort_keys=True)
    rcept_no = row["rcept_no"].strip()
    return PITRecord(
        entity_id=row["corp_code"].strip(), series_id=SERIES_FILINGS, metric_id=f"filing|{rcept_no}",
        available_at_utc=available_at(rcept_dt, ingested_at_utc), ingested_at_utc=ingested_at_utc,
        revision_id=f"{rcept_no}#{hashlib.sha256(value.encode()).hexdigest()[:12]}", value=value, unit="json",
        source_provider=PROVIDER, raw_payload_hash=raw_sha, license_contract_id=contract_id,
        observation_period_start=rcept_dt, observation_period_end=rcept_dt, vintage_date=rcept_dt,
        source_document_id=rcept_no, source_url=VIEWER_URL.format(rcept_no))


@dataclass(frozen=True)
class FilingRef:
    rcept_no: str
    corp_code: str
    rcept_dt: date
    period_end: date


@dataclass(frozen=True)
class FsConversion:
    records: list
    unchanged_by_correction: int     # 정정 보고서인데 값이 그대로라 새 버전을 만들지 않은 계정 수
    older_than_stored: int           # 이미 더 새 보고서의 값이 있어 넣지 않은 계정 수
    parse_errors: list


def fs_records(rows: list, *, filing: FilingRef, fs_div: str, contract_id: str, raw_sha: str,
               ingested_at_utc: datetime, latest: Callable[[tuple], Optional[PITRecord]]) -> FsConversion:
    """재무제표 응답 → 계정별 PIT 레코드. 정정 보고서는 값이 바뀐 계정만 이전 버전을 가리키는 새 revision으로 넣는다.

    metric_id = 연결구분|재무제표|계정ID|계정명|당기 또는 당기누적. 같은 응답 안에서 이름이 겹치면 #2, #3을 붙인다.
    기간: observation_period_end = 보고 기간 말일. 3개월·누적 구분은 metric_id에 있다.
    """
    avail = available_at(filing.rcept_dt, ingested_at_utc)
    seen, out, same, older, errors = Counter(), [], 0, 0, []
    for row in rows:
        if row.get("sj_div") not in FS_STATEMENTS:
            continue
        base = "|".join([fs_div, row["sj_div"], (row.get("account_id") or "").strip(), (row.get("account_nm") or "").strip()])
        for basis, key in (("당기", "thstrm_amount"), ("당기누적", "thstrm_add_amount")):
            try:
                value = parse_amount(row.get(key))
            except ValueError:
                errors.append(f"{filing.rcept_no} {base} {key}={row.get(key)!r}")
                continue
            if value is None:
                continue
            metric = f"{base}|{basis}"
            seen[metric] += 1
            if seen[metric] > 1:
                metric = f"{metric}#{seen[metric]}"
            currency = (row.get("currency") or "").strip() or None
            rec = PITRecord(
                entity_id=filing.corp_code, series_id=SERIES_FS, metric_id=metric, available_at_utc=avail,
                ingested_at_utc=ingested_at_utc, revision_id=f"{filing.rcept_no}|{metric}", value=value,
                unit=currency or "KRW", currency=currency, source_provider=PROVIDER, raw_payload_hash=raw_sha,
                license_contract_id=contract_id, observation_period_end=filing.period_end,
                vintage_date=filing.rcept_dt, source_document_id=filing.rcept_no,
                source_url=VIEWER_URL.format(filing.rcept_no))
            prev = latest(rec.observation_key)
            if prev is not None and prev.source_document_id != filing.rcept_no:
                if (prev.source_document_id or "") > filing.rcept_no:
                    older += 1          # 더 늦게 접수된 보고서의 값이 이미 있다
                    continue
                if prev.value == value:
                    same += 1
                    continue
                rec = replace(rec, supersedes_revision_id=prev.revision_id)
            out.append(rec)
    return FsConversion(out, same, older, errors)
