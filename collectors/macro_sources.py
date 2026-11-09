"""산업·거시 공공 데이터 수집: 관세청 품목별 수출입실적, 한국은행 ECOS, 미국 EIA (계약·판단은 DATA_SOURCES.md).

세 API 모두 최신 값만 주고 처음 발표된 값·발표 시각을 주지 않는다. 그래서 가용 시각은 실제로 받은 시각 이후 첫
08:30 KST 컷오프로 두고, 값이 바뀌면 이전 버전을 가리키는 새 revision으로 남긴다. 수집 시작 전의 과거 값도 같은
규칙이라 과거 시점의 조회에는 나오지 않는다 (나중에 받은 값으로 과거를 채우지 않는다).

- 관세청: apis.data.go.kr/1220000/Itemtrade/getItemtradeList (XML). 인증키는 serviceKey 파라미터.
- ECOS: ecos.bok.or.kr/api/StatisticSearch/{인증키}/json/... — 인증키가 주소 경로에 들어가 클라이언트가 주소에서 지운다.
  통계작성기관(ORG_NAME)이 한국은행이 아닌 통계표는 받지 않는다 (약관 제5조, DATA_SOURCES.md).
- EIA: api.eia.gov/v2/<route>/data/ (JSON). 인증키는 api_key 파라미터.
"""
import calendar as _calendar
import hashlib
import json
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from collectors.base_client import BaseClient, CallBudget, CollectorError, HttpResponse, TokenBucket
from core.calendar_align import first_kr_cutoff_at_or_after
from core.pit_schema import PITRecord

SERIES_KCS, SERIES_ECOS, SERIES_EIA = "kcs_itemtrade", "bok_ecos", "eia"
BOK_ORG_NAMES = (None, "", "한국은행")       # ECOS 통계표의 ORG_NAME이 이 값일 때만 받는다


class SourceError(CollectorError):
    def __init__(self, source: str, code: str, message: str, *, auth: bool = False, no_data: bool = False):
        super().__init__(f"{source}: {code} {message}".strip())
        self.source, self.code, self.message, self.auth, self.no_data = source, code, message, auth, no_data


@dataclass(frozen=True)
class Observation:
    series_key: str           # 예: HS8542, 722Y001/0101000, WCESTUS1
    metric: str               # 예: export_usd, value
    period_start: date
    period_end: date
    value: Optional[float]
    unit: str
    label: str


def month_bounds(y: int, m: int) -> tuple[date, date]:
    return date(y, m, 1), date(y, m, _calendar.monthrange(y, m)[1])


def _num(s) -> Optional[float]:
    if s is None:
        return None
    t = str(s).replace(",", "").strip()
    if t in ("", "-", "null"):
        return None
    return float(t)


# ---------------- 관세청 ----------------

KCS_METRICS = {"expDlr": ("export_usd", "USD"), "impDlr": ("import_usd", "USD"),
               "expWgt": ("export_kg", "KG"), "impWgt": ("import_kg", "KG")}


def parse_kcs(body: bytes, hs_code: str) -> list[Observation]:
    """XML 응답 → 월별 관측. 총계 행은 뺀다. 오류 응답(게이트웨이 형식 포함)은 SourceError."""
    root = ET.fromstring(body)
    if root.tag == "OpenAPI_ServiceResponse":                     # 게이트웨이 오류 (키 없음·미등록 등)
        msg = root.findtext(".//errMsg") or ""
        auth_msg = root.findtext(".//returnAuthMsg") or ""
        raise SourceError("KCS", root.findtext(".//returnReasonCode") or "gw", f"{msg} {auth_msg}".strip(),
                          auth="KEY" in (msg + auth_msg).upper())
    code = (root.findtext(".//header/resultCode") or "").strip()
    if code not in ("00", "0", "000"):
        raise SourceError("KCS", code or "?", root.findtext(".//header/resultMsg") or "",
                          no_data=code in ("03",))
    out = []
    for item in root.iter("item"):
        period = (item.findtext("year") or "").strip()
        if "." not in period:                                     # '총계' 등
            continue
        y, m = (int(x) for x in period.split(".")[:2])
        start, end = month_bounds(y, m)
        label = (item.findtext("statKor") or "").strip()
        for tag, (metric, unit) in KCS_METRICS.items():
            value = _num(item.findtext(tag))
            out.append(Observation(f"HS{hs_code}", metric, start, end, value, unit, label))
    return out


class KcsClient:
    BASE = "https://apis.data.go.kr/1220000/Itemtrade"

    def __init__(self, key: str, *, budget: CallBudget, rate: Optional[TokenBucket] = None, **http_kw):
        # 공공데이터포털은 인코딩 키(%2B…)와 디코딩 키를 함께 준다. 어느 쪽이든 디코딩 형태로 바꿔 한 번만 인코딩되게 한다
        self._key = urllib.parse.unquote(key)
        self.http = BaseClient(self.BASE, secret_params=("serviceKey",), secret_values=(key, self._key,
                               urllib.parse.quote(self._key, safe="")), budget=budget,
                               rate=rate if rate is not None else TokenBucket(2.0), **http_kw)

    def itemtrade(self, hs_code: str, start_yymm: str, end_yymm: str) -> tuple[HttpResponse, list[Observation]]:
        r = self.http.get("getItemtradeList", {"serviceKey": self._key, "strtYymm": start_yymm, "endYymm": end_yymm,
                                               "hsSgn": hs_code})
        return r, parse_kcs(r.body, hs_code)


# ---------------- ECOS ----------------

def _ecos_period(time: str, cycle: str) -> tuple[date, date]:
    t = time.strip()
    if cycle == "D":
        d = datetime.strptime(t, "%Y%m%d").date()
        return d, d
    if cycle == "M":
        return month_bounds(int(t[:4]), int(t[4:6]))
    if cycle == "Q":
        y, q = int(t[:4]), int(t[-1])
        return date(y, 3 * q - 2, 1), month_bounds(y, 3 * q)[1]
    if cycle == "A":
        return date(int(t), 1, 1), date(int(t), 12, 31)
    raise ValueError(f"알 수 없는 주기 {cycle}")


def _ecos_json(body: bytes, section: str) -> dict:
    d = json.loads(body.decode("utf-8"))
    if section in d:
        return d[section]
    res = d.get("RESULT", {})
    code, msg = str(res.get("CODE", "?")), str(res.get("MESSAGE", ""))
    raise SourceError("ECOS", code, msg, auth=code in ("INFO-100",), no_data=code == "INFO-200")


def parse_ecos(body: bytes, stat: str, item: str, cycle: str) -> list[Observation]:
    out = []
    for r in _ecos_json(body, "StatisticSearch").get("row", []):
        start, end = _ecos_period(r["TIME"], cycle)
        out.append(Observation(f"{stat}/{item}", "value", start, end, _num(r.get("DATA_VALUE")),
                               r.get("UNIT_NAME") or "", r.get("ITEM_NAME1") or ""))
    return out


class EcosClient:
    BASE = "https://ecos.bok.or.kr/api"

    def __init__(self, key: str, *, budget: CallBudget, rate: Optional[TokenBucket] = None, **http_kw):
        self._key = key
        self.http = BaseClient(self.BASE, secret_values=(key,), budget=budget,
                               rate=rate if rate is not None else TokenBucket(2.0), **http_kw)

    def table_org(self, stat: str) -> tuple[HttpResponse, Optional[str], str]:
        """통계표의 통계작성기관(ORG_NAME)과 이름."""
        r = self.http.get(f"StatisticTableList/{self._key}/json/kr/1/10/{stat}", {})
        rows = [x for x in _ecos_json(r.body, "StatisticTableList").get("row", []) if x.get("STAT_CODE") == stat]
        if not rows:
            raise SourceError("ECOS", "NO_TABLE", f"통계표 {stat} 없음")
        return r, rows[0].get("ORG_NAME"), rows[0].get("STAT_NAME") or ""

    def items(self, stat: str, start: int = 1, end: int = 1000) -> tuple[HttpResponse, list]:
        r = self.http.get(f"StatisticItemList/{self._key}/json/kr/{start}/{end}/{stat}", {})
        return r, _ecos_json(r.body, "StatisticItemList").get("row", [])

    def search(self, stat: str, cycle: str, start: str, end: str, item: str) -> tuple[HttpResponse, list[Observation]]:
        r = self.http.get(f"StatisticSearch/{self._key}/json/kr/1/10000/{stat}/{cycle}/{start}/{end}/{item}", {})
        return r, parse_ecos(r.body, stat, item, cycle)


# ---------------- EIA ----------------

def parse_eia(body: bytes, series: str, frequency: str) -> list[Observation]:
    d = json.loads(body.decode("utf-8"))
    if "error" in d and "response" not in d:
        raise SourceError("EIA", str(d.get("code", "?")), str(d.get("error")), auth="api_key" in str(d.get("error")))
    out = []
    for r in d.get("response", {}).get("data", []):
        if r.get("series") not in (None, series):
            continue
        p = str(r["period"])
        if frequency == "weekly":                    # 기간은 주의 마지막 날
            end = date.fromisoformat(p[:10])
            start = end - timedelta(days=6)
        elif frequency == "monthly":
            start, end = month_bounds(int(p[:4]), int(p[5:7]))
        else:
            start = end = date.fromisoformat(p[:10])
        out.append(Observation(series, "value", start, end, _num(r.get("value")), r.get("units") or "",
                               r.get("series-description") or r.get("process-name") or series))
    return out


class EiaClient:
    BASE = "https://api.eia.gov/v2"

    def __init__(self, key: str, *, budget: CallBudget, rate: Optional[TokenBucket] = None, **http_kw):
        self._key = key
        self.http = BaseClient(self.BASE, secret_params=("api_key",), secret_values=(key,), budget=budget,
                               rate=rate if rate is not None else TokenBucket(2.0), **http_kw)

    def series(self, route: str, series: str, frequency: str, start: str, length: int = 500):
        r = self.http.get(f"{route.strip('/')}/data/", {
            "api_key": self._key, "frequency": frequency, "data[0]": "value", "facets[series][]": series,
            "start": start, "sort[0][column]": "period", "sort[0][direction]": "desc", "length": length})
        return r, parse_eia(r.body, series, frequency)


# ---------------- PIT 레코드 ----------------

def observation_records(obs: list[Observation], *, series_id: str, entity_id: str, provider: str, contract_id: str,
                        raw_sha: str, ingested_at_utc: datetime, source_url: str,
                        latest: Callable[[tuple], Optional[PITRecord]]) -> tuple[list, int]:
    """관측 → PIT 레코드. 같은 값을 다시 받으면 넣지 않고(건너뛴 수로 셈), 값이 바뀌면 이전 버전을 가리킨다.
    값이 없는(결측) 관측은 넣지 않는다. 반환: (레코드, 값이 같아 건너뛴 수)."""
    avail = first_kr_cutoff_at_or_after(ingested_at_utc).to_pydatetime()
    out, same, seen = [], 0, set()
    for o in obs:
        if o.value is None:
            continue
        metric = f"{o.series_key}|{o.metric}"
        key = (metric, o.period_start, o.period_end)
        if key in seen:                          # 같은 응답 안의 중복 행
            continue
        seen.add(key)
        vhash = hashlib.sha256(repr(o.value).encode()).hexdigest()[:10]
        rec = PITRecord(entity_id=entity_id, series_id=series_id, metric_id=metric, available_at_utc=avail,
                        ingested_at_utc=ingested_at_utc, revision_id=f"{metric}|{o.period_end}|{vhash}",
                        value=o.value, unit=o.unit, source_provider=provider, raw_payload_hash=raw_sha,
                        license_contract_id=contract_id, observation_period_start=o.period_start,
                        observation_period_end=o.period_end, vintage_date=ingested_at_utc.date(),
                        source_url=source_url, attributes=json.dumps({"label": o.label}, ensure_ascii=False))
        prev = latest(rec.observation_key)
        if prev is not None:
            if prev.value == rec.value:          # 다시 받았는데 값이 그대로 → 넣지 않는다
                same += 1
                continue
            # 값이 바뀜: 예전에 같은 값을 받은 적이 있어도(A→B→A) 새 버전으로 남도록 받은 시각을 붙인다
            rec = replace(rec, revision_id=f"{rec.revision_id}@{ingested_at_utc:%Y%m%dT%H%M%S}",
                          supersedes_revision_id=prev.revision_id)
        out.append(rec)
    return out, same
