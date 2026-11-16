"""OpenDART 과거 재무 채우기 (2015년~): 정기보고서 목록 + 다중회사 주요계정. 계약: opendart_list_v1, opendart_fnltt_v1.

시점 기준(PIT) 규칙 — 지금 받는 과거 값은 그 뒤 정정된 최신 값일 수 있다:
- 값은 다중회사 주요계정(fnlttMultiAcnt.json, 100개 회사씩)으로 받는다. 이 값은 "받는 지금 시점의 최신본"으로 본다.
- 그래서 가용 시각은 그 보고서의 **마지막 재무 변경일** = max(응답 접수번호 날짜, 공시 목록의 마지막 [기재정정]
  접수일, 원래 제출일) 다음 KRX 세션 08:30이다. 정정 전 기간에는 값을 쓰지 않는다 (보수적, 미래 정보 없음).
- 이 시각은 provider_available_at_utc에 둔다. available_at_utc는 받은 시각 기준 그대로다
  (백테스트는 core.availability의 규칙으로 provider 시각을 쓴다).

대상: 12월 결산 회사의 표준 보고서만 (사업 12월·반기 6월·1분기 3월·3분기 9월). 다른 결산월은 건너뛰고 센다.
공시 목록은 시장 구분 없이 정기공시 전체를 받는다 (시장 구분으로 거르면 이미 상장폐지된 회사가 빠질 수 있다).
종목코드가 없는 회사(비상장, 또는 지금 종목코드가 지워진 상장폐지 회사)는 시세와 이을 수 없어 건너뛰고 센다.
"""
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Iterable, Optional

from collectors.opendart import NO_DATA, parse_amount, parse_periodic, parse_rcept_dt
from core.calendar_align import dart_available_at, first_kr_cutoff_at_or_after
from core.pit_schema import PITRecord

SERIES_REPORT_INDEX, SERIES_FIN_HIST = "dart_periodic_index", "dart_fin_hist"
STANDARD = {("사업보고서", 12): "11011", ("반기보고서", 6): "11012", ("분기보고서", 3): "11013", ("분기보고서", 9): "11014"}
ACCOUNTS = {"매출액": "revenue", "영업이익": "op_income", "법인세차감전 순이익": "pretax", "당기순이익(손실)": "net_income",
            "자산총계": "assets", "부채총계": "liabilities", "자본총계": "equity", "유동자산": "cur_assets",
            "유동부채": "cur_liab", "이익잉여금": "retained", "자본금": "paid_in"}
SOURCE_URL = "https://opendart.fss.or.kr"


def windows(start: date, end: date, days: int = 89):
    d = start
    while d <= end:
        e = min(d + timedelta(days=days), end)
        yield d, e
        d = e + timedelta(days=1)


@dataclass(frozen=True)
class ReportRow:
    corp_code: str
    stock_code: str
    corp_name: str
    kind: str
    period_end: date
    tags: tuple
    rcept_no: str
    rcept_dt: date

    @property
    def key(self) -> Optional[tuple]:
        """(corp_code, bsns_year, reprt_code). 12월 결산 표준 보고서가 아니면 None."""
        code = STANDARD.get((self.kind, self.period_end.month))
        return (self.corp_code, self.period_end.year, code) if code else None

    @property
    def changes_financials(self) -> bool:
        return not self.tags or "기재정정" in self.tags


def report_rows(list_rows: Iterable[dict]) -> tuple[list, dict]:
    """공시 목록 행 → 정기보고서 행. counts: 정기보고서 아님·비표준 결산·종목코드 없음."""
    out, counts = [], {"not_periodic": 0, "non_standard": 0, "no_stock_code": 0}
    for r in list_rows:
        rep = parse_periodic(r.get("report_nm", ""))
        if rep is None:
            counts["not_periodic"] += 1
            continue
        row = ReportRow(r["corp_code"].strip(), (r.get("stock_code") or "").strip(), (r.get("corp_name") or "").strip(),
                        rep.kind, rep.period_end, rep.tags, r["rcept_no"].strip(), parse_rcept_dt(r["rcept_dt"]))
        if row.key is None:
            counts["non_standard"] += 1
            continue
        if not row.stock_code:
            counts["no_stock_code"] += 1
            continue
        out.append(row)
    return out, counts


def last_change(rows: Iterable[ReportRow]) -> dict:
    """보고서 키 → 재무 숫자가 마지막으로 나오거나 바뀐 접수일 (최초 제출·[기재정정] 중 가장 늦은 것)."""
    out = {}
    for r in rows:
        if r.changes_financials and r.key is not None:
            out[r.key] = max(out.get(r.key, r.rcept_dt), r.rcept_dt)
    return out


def _avail(day: date, ingested_at_utc: datetime) -> tuple[datetime, datetime]:
    prov = dart_available_at(day).to_pydatetime()
    return prov, max(prov, first_kr_cutoff_at_or_after(ingested_at_utc).to_pydatetime())


def index_records(rows: list, *, contract_id: str, raw_sha: str, ingested_at_utc: datetime,
                  latest: Callable[[tuple], Optional[PITRecord]]) -> list:
    """정기보고서 목록 → PIT 레코드 (보고서 접수 한 건 = 레코드 하나, 바뀌지 않는 사실)."""
    out = []
    for r in rows:
        prov, avail = _avail(r.rcept_dt, ingested_at_utc)
        value = json.dumps({"kind": r.kind, "period_end": r.period_end.isoformat(), "tags": list(r.tags),
                            "stock_code": r.stock_code, "rcept_dt": r.rcept_dt.isoformat(), "reprt_code": r.key[2],
                            "bsns_year": r.key[1]}, ensure_ascii=False, sort_keys=True)
        rec = PITRecord(entity_id=r.corp_code, series_id=SERIES_REPORT_INDEX, metric_id=f"report|{r.rcept_no}",
                        available_at_utc=avail, ingested_at_utc=ingested_at_utc, revision_id=f"{r.corp_code}|{r.rcept_no}",
                        value=value, unit="json", source_provider="OpenDART", raw_payload_hash=raw_sha,
                        license_contract_id=contract_id, observation_period_start=r.period_end,
                        observation_period_end=r.period_end, provider_available_at_utc=prov, source_url=SOURCE_URL,
                        attributes=json.dumps({"corp_name": r.corp_name}, ensure_ascii=False))
        if latest(rec.observation_key) is None:
            out.append(rec)
    return out


def rows_from_records(records: Iterable[PITRecord]) -> list:
    """저장된 정기보고서 목록 레코드(index_records) → ReportRow. 목록을 다시 훑지 않고 값 받기만 할 때 쓴다."""
    out = []
    for rec in records:
        if rec.series_id != SERIES_REPORT_INDEX:
            continue
        v = json.loads(rec.value)
        name = json.loads(rec.attributes).get("corp_name", "") if rec.attributes else ""
        out.append(ReportRow(rec.entity_id, v["stock_code"], name, v["kind"], date.fromisoformat(v["period_end"]),
                             tuple(v["tags"]), rec.metric_id.split("|", 1)[1], date.fromisoformat(v["rcept_dt"])))
    return sorted(out, key=lambda r: (r.rcept_dt, r.rcept_no))


def fin_values(api_rows: list) -> dict:
    """한 응답의 행들 → 회사마다 {fs_div, rcept_no, stock_code, accounts{key: {amt, cum, prev_cum}}}.
    연결(CFS)이 있으면 연결, 없으면 별도(OFS)."""
    by_corp: dict[str, list] = {}
    for r in api_rows:
        by_corp.setdefault(r["corp_code"].strip(), []).append(r)
    out = {}
    for corp, rows in by_corp.items():
        fs = "CFS" if any(r.get("fs_div") == "CFS" for r in rows) else "OFS"
        acc = {}
        for r in rows:
            if r.get("fs_div") != fs or r.get("account_nm") not in ACCOUNTS:
                continue
            k = ACCOUNTS[r["account_nm"]]
            if k in acc:
                continue
            acc[k] = {"amt": parse_amount(r.get("thstrm_amount")), "cum": parse_amount(r.get("thstrm_add_amount")),
                      "prev_cum": parse_amount(r.get("frmtrm_add_amount")), "prev": parse_amount(r.get("frmtrm_amount"))}
        out[corp] = {"fs_div": fs, "rcept_no": rows[0].get("rcept_no", "").strip(),
                     "stock_code": (rows[0].get("stock_code") or "").strip(), "accounts": acc}
    return out


def fin_records(values: dict, bsns_year: int, reprt_code: str, period_end: date, changes: dict, *,
                contract_id: str, raw_sha: str, ingested_at_utc: datetime,
                latest: Callable[[tuple], Optional[PITRecord]]) -> tuple[list, dict]:
    out, counts = [], {"unchanged": 0, "revised": 0, "no_accounts": 0}
    for corp, v in sorted(values.items()):
        if not v["accounts"]:
            counts["no_accounts"] += 1
            continue
        api_day = parse_rcept_dt(v["rcept_no"][:8]) if len(v["rcept_no"]) >= 8 else None
        version = max(d for d in (api_day, changes.get((corp, bsns_year, reprt_code))) if d is not None)
        prov, avail = _avail(version, ingested_at_utc)
        value = json.dumps({"fs_div": v["fs_div"], "accounts": v["accounts"]}, ensure_ascii=False, sort_keys=True)
        h = hashlib.sha256(value.encode()).hexdigest()[:10]
        rec = PITRecord(entity_id=corp, series_id=SERIES_FIN_HIST, metric_id=f"fin|{reprt_code}",
                        available_at_utc=avail, ingested_at_utc=ingested_at_utc,
                        revision_id=f"{corp}|{bsns_year}|{reprt_code}|{h}", value=value, unit="json",
                        source_provider="OpenDART", raw_payload_hash=raw_sha, license_contract_id=contract_id,
                        observation_period_start=date(bsns_year, 1, 1), observation_period_end=period_end,
                        provider_available_at_utc=prov, currency="KRW", source_url=SOURCE_URL,
                        attributes=json.dumps({"stock_code": v["stock_code"], "api_rcept_no": v["rcept_no"],
                                               "version_date": version.isoformat()}, ensure_ascii=False, sort_keys=True))
        prev = latest(rec.observation_key)
        if prev is not None:
            if prev.value == rec.value:
                counts["unchanged"] += 1
                continue
            counts["revised"] += 1
            from dataclasses import replace
            rec = replace(rec, revision_id=f"{rec.revision_id}@{ingested_at_utc:%Y%m%dT%H%M%S}",
                          supersedes_revision_id=prev.revision_id)
        out.append(rec)
    return out, counts


def period_end_of(bsns_year: int, reprt_code: str) -> date:
    return {"11013": date(bsns_year, 3, 31), "11012": date(bsns_year, 6, 30), "11014": date(bsns_year, 9, 30),
            "11011": date(bsns_year, 12, 31)}[reprt_code]


def is_empty(result) -> bool:
    return result.status == NO_DATA
