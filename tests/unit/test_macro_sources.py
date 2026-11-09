"""관세청·한국은행 ECOS·EIA 수집기. 응답 형식은 ECOS 샘플 키 실제 응답과 관세청·EIA 공개 명세를 본떴다 (값은 꾸민 숫자)."""
import json
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest

from collectors.base_client import CallBudget, TokenBucket
from collectors.macro_sources import (EcosClient, KcsClient, SourceError, observation_records, parse_ecos, parse_eia,
                                      parse_kcs)
from core.pit_schema import PITStore
from core.source_registry import default_registry
from orchestration.macro_daily import MacroRun
from storage.pit_store import PITFileStore

UTC = timezone.utc
KCS_KEY, ECOS_KEY, EIA_KEY = "kcs+KEY/abc=", "ECOSKEY12345", "EIAKEY987"

KCS_OK = """<?xml version="1.0" encoding="UTF-8"?><response><header><resultCode>00</resultCode><resultMsg>NORMAL SERVICE.</resultMsg></header>
<body><items>
<item><balPayments>100</balPayments><expDlr>12,000</expDlr><expWgt>50</expWgt><hsCode>8542</hsCode><impDlr>5000</impDlr><impWgt>20</impWgt><statKor>전자집적회로</statKor><year>2026.08</year></item>
<item><balPayments>200</balPayments><expDlr>13000</expDlr><expWgt>55</expWgt><hsCode>8542</hsCode><impDlr>5100</impDlr><impWgt>21</impWgt><statKor>전자집적회로</statKor><year>2026.09</year></item>
<item><balPayments>300</balPayments><expDlr>25000</expDlr><expWgt>105</expWgt><hsCode>-</hsCode><impDlr>10100</impDlr><impWgt>41</impWgt><statKor>-</statKor><year>총계</year></item>
</items></body></response>""".encode()
KCS_GW_ERR = """<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR</errMsg><returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg><returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>""".encode()


def ecos_rows(stat, item, times, values, unit="연%", name="한국은행 기준금리"):
    return {"StatisticSearch": {"list_total_count": len(times), "row": [
        {"STAT_CODE": stat, "STAT_NAME": "x", "ITEM_CODE1": item, "ITEM_NAME1": name, "UNIT_NAME": unit, "TIME": t,
         "DATA_VALUE": v} for t, v in zip(times, values)]}}


def eia_rows(series, periods, values):
    return {"response": {"total": str(len(periods)), "frequency": "weekly", "data": [
        {"period": p, "series": series, "series-description": "Weekly U.S. Ending Stocks", "value": v, "units": "MBBL"}
        for p, v in zip(periods, values)]}}


class Fake:
    """호스트별로 응답을 돌려주는 가짜 전송. 받은 요청(주소·파라미터)을 기록한다."""

    def __init__(self):
        self.calls = []
        self.kcs = KCS_OK
        self.ecos_org = {"722Y001": None, "402Y014": "한국은행", "817Y002": "한국은행", "731Y001": "서울외국환중개"}
        self.ecos = {"722Y001/0101000": ecos_rows("722Y001", "0101000", ["202608", "202609"], ["2.5", "2.25"])}
        self.eia = eia_rows("WCESTUS1", ["2026-10-30", "2026-10-23"], [421000, 419500])
        self.eia_status = 200

    def __call__(self, url, params, timeout):
        self.calls.append((url, dict(params)))
        u = urlparse(url)
        if u.netloc == "apis.data.go.kr":
            return 200, {}, self.kcs
        if u.netloc == "ecos.bok.or.kr":
            parts = u.path.split("/")                 # /api/<서비스>/<키>/json/kr/<시작>/<끝>/<통계표>/...
            service, key, stat = parts[2], parts[3], parts[8]
            if key != ECOS_KEY:
                return 200, {}, json.dumps({"RESULT": {"CODE": "INFO-100", "MESSAGE": "인증키가 유효하지 않습니다."}}).encode()
            if service == "StatisticTableList":
                return 200, {}, json.dumps({"StatisticTableList": {"row": [
                    {"STAT_CODE": stat, "STAT_NAME": f"표 {stat}", "ORG_NAME": self.ecos_org.get(stat)}]}}).encode()
            item = parts[12]
            body = self.ecos.get(f"{stat}/{item}") or {"RESULT": {"CODE": "INFO-200", "MESSAGE": "해당하는 데이터가 없습니다."}}
            return 200, {}, json.dumps(body, ensure_ascii=False).encode()
        if u.netloc == "api.eia.gov":
            if self.eia_status != 200:
                return self.eia_status, {}, b'{"error":"invalid api_key"}'
            return 200, {}, json.dumps(self.eia).encode()
        return 404, {}, b""


CONFIG = {"kcs": {"months_back": 2, "hs_codes": [{"code": "8542", "label": "반도체"}]},
          "ecos": {"series": [{"stat": "722Y001", "cycle": "M", "item": "0101000", "label": "기준금리", "back": 3},
                              {"stat": "731Y001", "cycle": "D", "item": "0000001", "label": "원/달러", "back": 5},
                              {"stat": "402Y014", "cycle": "M", "item": "999", "label": "없는 품목", "back": 3}]},
          "eia": {"weeks_back": 4, "series": [{"route": "petroleum/stoc/wstk", "series": "WCESTUS1",
                                                "frequency": "weekly", "label": "원유 재고"}]}}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make_run(tmp_path, fake, clock, keys=None):
    return MacroRun(tmp_path, keys or {"kcs": KCS_KEY, "ecos": ECOS_KEY, "eia": EIA_KEY}, config=CONFIG,
                    registry=default_registry(), now=clock, version="test",
                    http_kw={"transport": fake, "sleep": lambda s: None, "rate": TokenBucket(1e9)})


# ---------- 응답 해석 ----------

def test_parse_kcs_months_and_errors():
    obs = parse_kcs(KCS_OK, "8542")
    exp = {(o.period_end, o.metric): o.value for o in obs}
    assert exp[(date(2026, 9, 30), "export_usd")] == 13000 and exp[(date(2026, 8, 31), "export_usd")] == 12000
    assert {o.period_start for o in obs} == {date(2026, 8, 1), date(2026, 9, 1)}            # 총계 행 제외
    with pytest.raises(SourceError) as e:
        parse_kcs(KCS_GW_ERR, "8542")
    assert e.value.auth
    with pytest.raises(SourceError) as e:
        parse_kcs(b"<response><header><resultCode>03</resultCode><resultMsg>NO DATA</resultMsg></header></response>", "1")
    assert e.value.no_data


def test_parse_ecos_and_eia():
    obs = parse_ecos(json.dumps(ecos_rows("722Y001", "0101000", ["202609"], ["2.25"])).encode(), "722Y001", "0101000", "M")
    assert (obs[0].series_key, obs[0].period_start, obs[0].period_end, obs[0].value) == \
        ("722Y001/0101000", date(2026, 9, 1), date(2026, 9, 30), 2.25)
    with pytest.raises(SourceError) as e:
        parse_ecos(b'{"RESULT":{"CODE":"INFO-200","MESSAGE":"x"}}', "a", "b", "M")
    assert e.value.no_data
    w = parse_eia(json.dumps(eia_rows("WCESTUS1", ["2026-10-30"], [421000])).encode(), "WCESTUS1", "weekly")
    assert (w[0].period_start, w[0].period_end, w[0].value) == (date(2026, 10, 24), date(2026, 10, 30), 421000)


# ---------- 인증키 처리 ----------

def test_ecos_key_in_path_is_scrubbed_and_kcs_key_normalized():
    fake = Fake()
    c = EcosClient(ECOS_KEY, budget=CallBudget(5), rate=TokenBucket(1e9), transport=fake, sleep=lambda s: None)
    r, org, _ = c.table_org("402Y014")
    assert org == "한국은행" and ECOS_KEY in fake.calls[-1][0] and ECOS_KEY not in r.url and "***" in r.url
    k = KcsClient("abc%2Bdef%3D", budget=CallBudget(5), rate=TokenBucket(1e9), transport=fake, sleep=lambda s: None)
    r, _ = k.itemtrade("8542", "202608", "202609")
    assert fake.calls[-1][1]["serviceKey"] == "abc+def="           # 인코딩 키를 넣어도 한 번만 인코딩되게
    assert r.params["serviceKey"] == "***"


# ---------- PIT 레코드 ----------

def test_observation_revisions_including_flip_flop(registry):
    store = PITStore(default_registry())
    t = datetime(2026, 11, 9, 22, 47, tzinfo=UTC)

    def ingest(value, when):
        obs = parse_kcs(KCS_OK.replace(b"13000", str(value).encode()), "8542")
        recs, same = observation_records(obs, series_id="kcs_itemtrade", entity_id="KR", provider="KCS",
                                         contract_id="kcs_itemtrade_v1", raw_sha="h", ingested_at_utc=when,
                                         source_url="u", latest=store.latest)
        for r in recs:
            store.append(r)
        return [r for r in recs if r.metric_id == "HS8542|export_usd" and r.observation_period_end == date(2026, 9, 30)], same

    first, _ = ingest(13000, t)
    assert len(first) == 1 and first[0].available_at_utc == datetime(2026, 11, 9, 23, 30, tzinfo=UTC)
    again, same = ingest(13000, t + timedelta(days=1))
    assert again == [] and same == 8                                          # 값이 같으면 새 버전 없음
    changed, _ = ingest(13500, t + timedelta(days=2))
    assert changed[0].supersedes_revision_id == first[0].revision_id
    back, _ = ingest(13000, t + timedelta(days=3))                            # A→B→A도 새 버전으로 남음
    assert back and back[0].revision_id != first[0].revision_id and back[0].supersedes_revision_id == changed[0].revision_id


# ---------- 전체 실행 ----------

def test_macro_run_collects_guards_org_and_never_stores_keys(tmp_path):
    fake, clock = Fake(), Clock(datetime(2026, 11, 9, 22, 47, tzinfo=UTC))
    res = make_run(tmp_path, fake, clock).run()
    assert res["kcs"].status == "ok" and res["kcs"].counts["added"] == 8
    e = res["ecos"]
    assert e.counts["added"] == 2 and e.counts["skipped_org"] == 1
    assert any("서울외국환중개" in w for w in e.warnings) and any("데이터 없음" in w for w in e.warnings)
    assert not any("731Y001/0000001" in c[0] and "StatisticSearch" in c[0] for c in fake.calls)   # 기관 확인 뒤 안 받음
    assert res["eia"].counts["added"] == 2

    store = PITFileStore(tmp_path, default_registry()).load()
    assert len(store) == 12
    for p in tmp_path.rglob("*"):
        if p.is_file() and p.suffix in (".json", ".jsonl"):
            text = p.read_text(encoding="utf-8")
            for k in (KCS_KEY, ECOS_KEY, EIA_KEY, "kcs+KEY"):
                assert k not in text, (p, k)

    clock.t += timedelta(days=1)                                              # 다음 날: 값이 같으면 추가 없음
    res2 = make_run(tmp_path, fake, clock).run()
    assert res2["kcs"].counts.get("added", 0) == 0 and res2["eia"].counts.get("added", 0) == 0


def test_missing_key_skips_source_and_auth_error_stops_it(tmp_path):
    fake, clock = Fake(), Clock(datetime(2026, 11, 9, 22, 47, tzinfo=UTC))
    res = make_run(tmp_path, fake, clock, keys={"ecos": "WRONG", "eia": EIA_KEY}).run()
    assert res["kcs"] is None                                                 # 키 없음 → 건너뜀
    assert res["ecos"].status == "failed" and "INFO-100" in res["ecos"].errors[0] and len(res["ecos"].errors) == 1
    fake.eia_status = 403
    res = make_run(tmp_path, fake, clock, keys={"eia": EIA_KEY}).run()
    assert res["eia"].status == "failed" and EIA_KEY not in res["eia"].errors[0]


def test_check_mode_reports_each_source(tmp_path):
    fake, clock = Fake(), Clock(datetime(2026, 11, 9, 22, 47, tzinfo=UTC))
    lines = make_run(tmp_path, fake, clock, keys={"ecos": ECOS_KEY, "eia": EIA_KEY}).check()
    assert lines[0] == "kcs: DATA_GO_KR_KEY 없음" and lines[1].startswith("ecos: 정상") and lines[2].startswith("eia: 정상")
    assert not any(tmp_path.iterdir())                                        # 저장 없음
