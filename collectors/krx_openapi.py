"""한국거래소 KRX Open API 일별매매정보 수집 (유가증권·코스닥·코넥스). 계약: krx_openapi_stock_v1 (DATA_SOURCES.md).

이용약관(2025-12-26 시행)에서 확인한 것: 비상업적 목적으로만 이용(제6조 ②), 제3자 제공 금지(제11조 ②), 이용계약 종료 뒤
이용 금지(제11조 ③, 인증키 이용기간 1년·연장 가능), 결과로 화면을 만들면 "한국거래소 통계정보" 표시(제10조 ③),
키당 하루 10,000회(제8조 ④). 그래서 비공개 저장소에만 두고, 키 만료일(KRX_KEY_VALID_UNTIL)이 지나면 받지 않는다.

공개 페이지에서 확인한 것: API ID(stk_bydd_trd · ksq_bydd_trd · knx_bydd_trd), 인증키는 요청 헤더 AUTH_KEY,
공개 샘플 주소 https://data-dbg.krx.co.kr/svc/sample/apis/sto/<API ID>, 유가증권·코스닥 2010-01-04부터.
**아직 확인하지 못한 것** (출력 필드표·개발 명세서는 로그인해야 보인다): 실제 주소(샘플 주소에서 /sample을 뺀 것으로
가정), 기준일 파라미터 이름(basDd로 가정), 응답 필드 이름(아래 FIELDS로 가정), T일 자료의 공개 시각.
키를 받으면 --probe로 확인하고 여기를 고친다. 필드 이름이 다르면 필수 필드 검사에서 멈추고 저장하지 않는다.

이 모듈과 실행 코드는 시세 값을 출력하지 않는다 (공개 저장소의 Actions 로그는 공개).
"""
import hashlib
import json
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from collectors.base_client import BaseClient, CallBudget, CollectorError, RequestFailed, TokenBucket, check_schema
from collectors.fsc_krx import Page, provider_available_at
from core.calendar_align import KR_VENUE, calendar, first_kr_cutoff_at_or_after, session_close_utc
from core.pit_schema import PITRecord

SERIES_KRX_PRICE = "krx_openapi_daily"
PROVIDER = "KRX"
SOURCE_URL = "https://openapi.krx.co.kr/contents/OPP/INFO/service/OPPINFO004.cmd"
ATTRIBUTION = "한국거래소 통계정보"
BASE_URL = "https://data-dbg.krx.co.kr/svc/apis/sto"          # 가정: 공개 샘플 주소에서 /sample 을 뺀 것
DATE_PARAM = "basDd"                                           # 가정
ROWS_KEY = "OutBlock_1"                                        # 가정
MARKETS = {"KOSPI": "stk_bydd_trd", "KOSDAQ": "ksq_bydd_trd", "KONEX": "knx_bydd_trd"}   # 공개 페이지에서 확인
# 응답 필드 → 저장 이름 (가정, probe로 확인). FSC 시세와 같은 저장 이름을 써서 두 공급원을 바로 대조할 수 있게 한다
FIELDS = {"TDD_OPNPRC": "open", "TDD_HGPRC": "high", "TDD_LWPRC": "low", "TDD_CLSPRC": "close",
          "ACC_TRDVOL": "volume", "ACC_TRDVAL": "value", "LIST_SHRS": "shares", "MKTCAP": "mktcap"}
ID_FIELDS = ("BAS_DD", "ISU_CD", "ISU_NM", "MKT_NM")
REQUIRED = (*ID_FIELDS, *FIELDS)
KNOWN = ("SECT_TP_NM", "CMPPREVDD_PRC", "FLUC_RT")
LICENSE_STATE = Path("state") / "krx_openapi_license.json"


class KrxApiError(CollectorError):
    def __init__(self, code: str, message: str, *, auth: bool = False):
        super().__init__(f"KRX Open API: {code} {message}".strip())
        self.code, self.message, self.auth = code, message, auth


def parse_rows(body: bytes) -> list:
    """JSON 응답 → 행 목록. 형식이 다르면 KrxApiError (응답의 키 이름만 알리고 값은 알리지 않는다)."""
    try:
        d = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise KrxApiError("parse", "JSON이 아닌 응답") from None
    if not isinstance(d, dict) or ROWS_KEY not in d:
        keys = sorted(d)[:10] if isinstance(d, dict) else type(d).__name__
        raise KrxApiError("format", f"응답에 {ROWS_KEY}가 없음 (응답 키: {keys})")
    rows = d[ROWS_KEY] or []
    if not isinstance(rows, list):
        raise KrxApiError("format", f"{ROWS_KEY}가 목록이 아님")
    return rows


class KrxOpenApiClient:
    def __init__(self, key: str, *, budget: CallBudget, rate: Optional[TokenBucket] = None, **http_kw):
        if not key:
            raise ValueError("KRX Open API 인증키(KRX_API_KEY)가 없음")
        self.budget = budget
        self.http = BaseClient(BASE_URL, headers={"AUTH_KEY": key}, budget=budget,
                               rate=rate if rate is not None else TokenBucket(2.0), **http_kw)

    def market_day(self, market: str, day: date) -> Page:
        try:
            r = self.http.get(MARKETS[market], {DATE_PARAM: f"{day:%Y%m%d}"})
        except RequestFailed as e:
            if e.status in (401, 403):
                raise KrxApiError(str(e.status), "인증키가 유효하지 않거나 이 API의 이용신청·승인이 없음", auth=True) from None
            raise
        rows = [dict(x, _market=market) for x in parse_rows(r.body)]
        return Page(r, rows, len(rows), 1)

    def prices(self, day: date) -> list:
        """기준일 하루의 세 시장 전 종목 (시장마다 응답 하나)."""
        return [self.market_day(m, day) for m in MARKETS]


def _int(s) -> Optional[int]:
    if s is None:
        return None
    t = str(s).replace(",", "").strip()
    if t in ("", "-"):
        return None
    return int(float(t)) if "." in t else int(t)


def available_at(day: date, ingested_at_utc: datetime) -> datetime:
    """보수적 가정: T일 자료는 다음 KRX 세션 13:00 KST 이후에 있다고 보고 그 뒤 첫 08:30 컷오프부터 쓴다 (FSC와 같은 규칙).
    실제 공개 시각을 수집 기록(아직 자료 없음/있음)으로 확인하면 앞당긴다. 늦게 받으면 받은 뒤 첫 컷오프."""
    rule = first_kr_cutoff_at_or_after(provider_available_at(day))
    return max(rule, first_kr_cutoff_at_or_after(ingested_at_utc)).to_pydatetime()


def price_records(rows: list, day: date, *, contract_id: str, raw_sha: str, ingested_at_utc: datetime,
                  latest: Callable[[tuple], Optional[PITRecord]]) -> tuple[list, dict]:
    """하루치 행 → 종목마다 레코드 하나 (값: {open, high, low, close, volume, value, shares, mktcap} JSON)."""
    cal = calendar(KR_VENUE)
    close = session_close_utc(pd.Timestamp(day), KR_VENUE).to_pydatetime() if cal.is_session(pd.Timestamp(day)) else None
    avail = available_at(day, ingested_at_utc)
    counts = {"rows": len(rows), "bad_rows": 0, "duplicates": 0, "unchanged": 0, "revised": 0}
    out, seen = [], set()
    for row in rows:
        try:
            code = str(row["ISU_CD"]).strip()
            if str(row["BAS_DD"]).strip() != f"{day:%Y%m%d}" or not code:
                raise ValueError("기준일·종목코드 이상")
            content = {name: _int(row[f]) for f, name in FIELDS.items()}
        except (KeyError, ValueError, AttributeError):
            counts["bad_rows"] += 1
            continue
        if code in seen:
            counts["duplicates"] += 1
            continue
        seen.add(code)
        value = json.dumps(content, sort_keys=True)
        attrs = {"code": code, "name": str(row["ISU_NM"]).strip(), "market": str(row["MKT_NM"]).strip(),
                 "section": str(row.get("SECT_TP_NM") or "").strip()}
        rec = PITRecord(entity_id=f"KRX:{code}", series_id=SERIES_KRX_PRICE, metric_id="ohlcv",
                        available_at_utc=avail, ingested_at_utc=ingested_at_utc,
                        revision_id=f"KRX:{code}|{day:%Y%m%d}|{hashlib.sha256(value.encode()).hexdigest()[:10]}",
                        value=value, unit="json", source_provider=PROVIDER, raw_payload_hash=raw_sha,
                        license_contract_id=contract_id, observation_period_start=day, observation_period_end=day,
                        event_time_utc=close, provider_available_at_utc=provider_available_at(day), currency="KRW",
                        source_url=SOURCE_URL, attributes=json.dumps(attrs, ensure_ascii=False, sort_keys=True))
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
    s = check_schema([{k: v for k, v in r.items() if k != "_market"} for r in rows], REQUIRED, KNOWN)
    return s.missing, s.unexpected


# ---------- 이용기간 (약관 제11조 ③: 이용계약 종료 뒤 이용 금지) ----------

def parse_valid_until(value: Optional[str]) -> date:
    if not value:
        raise KrxApiError("license", "KRX_KEY_VALID_UNTIL(인증키 이용기간 마지막 날, YYYY-MM-DD)이 없음 — "
                                     "이용기간을 모르면 받지 않는다 (약관 제11조 ③)")
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise KrxApiError("license", f"KRX_KEY_VALID_UNTIL 형식 오류: {value!r} (YYYY-MM-DD)") from None


def check_license(valid_until: date, today: date) -> None:
    if today > valid_until:
        raise KrxApiError("license", f"인증키 이용기간이 {valid_until}에 끝남 — 연장 전에는 받지도 쓰지도 않는다 "
                                     "(약관 제11조 ③). 연장하지 않으면 저장된 KRX Open API 데이터를 지워야 한다")


def record_license(data_dir, valid_until: date, now: datetime) -> Path:
    """비공개 저장소에 이용기간을 남긴다. 이 데이터를 읽는 코드(학습 등)는 license_ok()로 먼저 확인한다."""
    p = Path(data_dir) / LICENSE_STATE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"contract_id": "krx_openapi_stock_v1", "valid_until": valid_until.isoformat(),
                             "recorded_at_utc": now.isoformat(), "rule": "약관 제11조 ③: 이용계약 종료 뒤 이용 금지"},
                            ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return p


def license_ok(data_dir, today: date) -> bool:
    p = Path(data_dir) / LICENSE_STATE
    if not p.exists():
        return False
    return today <= date.fromisoformat(json.loads(p.read_text(encoding="utf-8"))["valid_until"])
