"""KRX Open API 일별매매정보 수집기, 이용기간 가드, 공급원 대조. 응답 형식은 가정(collectors/krx_openapi.py)을 본떴다.
실제 형식은 인증키 승인 뒤 --probe로 확인하고, 다르면 수집기와 이 테스트를 함께 고친다. 값은 꾸민 숫자다."""
import json
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest

from collectors import krx_openapi
from collectors.base_client import CallBudget, TokenBucket
from collectors.krx_openapi import (SERIES_KRX_PRICE, KrxApiError, KrxOpenApiClient, check_license, license_ok,
                                    parse_rows, parse_valid_until, record_license)
from core.data_contracts import Purpose
from core.source_registry import default_registry
from orchestration import compare_prices
from orchestration.krx_daily import KrxRun, fsc_source, krx_check, krx_probe, krx_source
from storage.visibility import PRIVATE_MARKER
from tests.unit.test_fsc_krx import Fake as FakeFsc
from tests.unit.test_fsc_krx import client as fsc_client

UTC = timezone.utc
KEY = "KRXKEY-abc123"
NOW = datetime(2026, 11, 10, 5, 23, tzinfo=UTC)


def krx_row(code, name, close, market="KOSPI", day="20261109", **kw):
    r = {"BAS_DD": day, "ISU_CD": code, "ISU_NM": name, "MKT_NM": market, "SECT_TP_NM": "",
         "TDD_CLSPRC": str(close), "CMPPREVDD_PRC": "100", "FLUC_RT": "0.5", "TDD_OPNPRC": str(close - 100),
         "TDD_HGPRC": str(close + 200), "TDD_LWPRC": str(close - 300), "ACC_TRDVOL": "1234567",
         "ACC_TRDVAL": "98765432100", "MKTCAP": str(close * 5919637922), "LIST_SHRS": "5919637922"}
    r.update(kw)
    return r


class FakeKrx:
    """API ID별로 행을 돌려주는 가짜 KRX Open API. 헤더 AUTH_KEY가 틀리면 401."""

    def __init__(self):
        self.data = {("stk_bydd_trd", "20261109"): [krx_row("005930", "삼성전자", 71000)],
                     ("ksq_bydd_trd", "20261109"): [krx_row("247540", "에코프로비엠", 150000, market="KOSDAQ")],
                     ("knx_bydd_trd", "20261109"): []}
        self.calls = []

    def __call__(self, url, params, timeout, headers=None):
        self.calls.append((urlparse(url).path, dict(params), dict(headers or {})))
        if (headers or {}).get("AUTH_KEY") != KEY:
            return 401, {}, b"Unauthorized"
        api = urlparse(url).path.rsplit("/", 1)[-1]
        return 200, {}, json.dumps({"OutBlock_1": self.data.get((api, params["basDd"]), [])}, ensure_ascii=False).encode()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def kclient(fake, clock, key=KEY):
    return KrxOpenApiClient(key, budget=CallBudget(50), rate=TokenBucket(1e9), transport=fake, sleep=lambda s: None,
                            now=clock, max_retries=0)


@pytest.fixture
def private_dir(tmp_path):
    (tmp_path / PRIVATE_MARKER).write_text("private\n")
    return tmp_path


def test_contract_allows_training_but_not_display_redistribution_or_llm():
    reg = default_registry()
    cid = "krx_openapi_stock_v1"
    for p in (Purpose.STORAGE, Purpose.ML_TRAINING, Purpose.DERIVED_USE):
        assert reg.is_allowed(cid, p)
    for p in (Purpose.PUBLIC_DISPLAY, Purpose.RAW_REDISTRIBUTION, Purpose.LLM_INPUT, Purpose.ARCHIVAL):
        assert not reg.is_allowed(cid, p)
    c = reg.get(cid)
    assert c.attribution_required and "제11조" in c.permission_reference


def test_client_sends_key_in_header_and_hides_it(private_dir):
    fake, clock = FakeKrx(), Clock(NOW)
    pages = kclient(fake, clock).prices(date(2026, 11, 9))
    assert [len(p.rows) for p in pages] == [1, 1]                               # 기본: 코스피·코스닥 (승인한 서비스)
    allm = KrxOpenApiClient(KEY, budget=CallBudget(9), rate=TokenBucket(1e9), transport=fake, sleep=lambda s: None,
                            now=clock, markets=("KOSPI", "KOSDAQ", "KONEX"))
    assert [len(p.rows) for p in allm.prices(date(2026, 11, 9))] == [1, 1, 0]
    with pytest.raises(ValueError, match="알 수 없는 시장"):
        KrxOpenApiClient(KEY, budget=CallBudget(1), markets=("NYSE",))
    path, params, headers = fake.calls[0]
    assert path.endswith("/svc/apis/sto/stk_bydd_trd") and params == {"basDd": "20261109"}
    assert headers["AUTH_KEY"] == KEY and KEY not in json.dumps(pages[0].response.params)
    with pytest.raises(KrxApiError) as e:
        kclient(FakeKrx(), clock, key="WRONG").market_day("KOSPI", date(2026, 11, 9))
    assert e.value.auth and "WRONG" not in str(e.value)
    with pytest.raises(KrxApiError, match="OutBlock_1"):
        parse_rows(b'{"respCode": "E01"}')


def test_run_stores_records_and_detects_schema_change(private_dir):
    fake, clock = FakeKrx(), Clock(NOW)
    m = KrxRun(krx_source(kclient(fake, clock)), private_dir, now=clock, version="t").run([date(2026, 11, 9)])
    assert m.status == "ok" and m.counts["added"] == 2 and m.target["source"] == "krx"
    from storage.pit_store import read_parquet
    rec = read_parquet(private_dir / "pit_versions" / SERIES_KRX_PRICE / "2026-11-09.parquet")[0]
    assert rec.entity_id == "KRX:005930" and rec.license_contract_id == "krx_openapi_stock_v1"
    assert json.loads(rec.value)["close"] == 71000 and rec.available_at_utc.isoformat().startswith("2026-11-10T23:30")
    for p in private_dir.rglob("*"):
        if p.is_file():
            assert KEY.encode() not in p.read_bytes()
    # 필드 이름이 가정과 다르면 저장하지 않고 멈춘다
    fake.data[("stk_bydd_trd", "20261110")] = [{"BAS_DD": "20261110", "ISU_CD": "005930", "CLOSE": "1"}]
    m2 = KrxRun(krx_source(kclient(fake, clock)), private_dir, now=clock, version="t").run([date(2026, 11, 10)])
    assert m2.status == "failed" and "필수 필드 없음" in m2.errors[0]
    assert not list((private_dir / "pit_versions" / SERIES_KRX_PRICE).glob("2026-11-10.*"))


def test_public_store_gets_nothing(tmp_path):
    fake, clock = FakeKrx(), Clock(NOW)
    m = KrxRun(krx_source(kclient(fake, clock)), tmp_path, now=clock, version="t").run([date(2026, 11, 9)])
    assert m.status == "failed" and fake.calls == [] and list(tmp_path.iterdir()) == []


def test_license_period_guard(private_dir):
    assert parse_valid_until("2027-11-14") == date(2027, 11, 14)
    with pytest.raises(KrxApiError, match="KRX_KEY_VALID_UNTIL"):
        parse_valid_until(None)
    with pytest.raises(KrxApiError, match="형식"):
        parse_valid_until("내년")
    check_license(date(2027, 11, 14), date(2027, 11, 14))                     # 마지막 날까지는 된다
    with pytest.raises(KrxApiError, match="제11조"):
        check_license(date(2027, 11, 14), date(2027, 11, 15))
    assert not license_ok(private_dir, date(2026, 11, 15))                     # 기록이 없으면 쓰지 않는다
    record_license(private_dir, date(2027, 11, 14), NOW)
    assert license_ok(private_dir, date(2027, 11, 14)) and not license_ok(private_dir, date(2027, 11, 15))


def test_cli_refuses_without_valid_period(private_dir, monkeypatch, capsys):
    from orchestration import krx_daily
    monkeypatch.setenv("KRX_API_KEY", KEY)
    monkeypatch.delenv("KRX_KEY_VALID_UNTIL", raising=False)
    assert krx_daily.main(["--source", "krx", "--data-dir", str(private_dir)]) == 1
    assert "KRX_KEY_VALID_UNTIL" in capsys.readouterr().err
    monkeypatch.setenv("KRX_KEY_VALID_UNTIL", "2020-01-01")
    assert krx_daily.main(["--source", "krx", "--data-dir", str(private_dir)]) == 1
    assert "이용기간" in capsys.readouterr().err


def test_check_and_probe_print_no_prices():
    fake, clock = FakeKrx(), Clock(NOW)
    text = krx_check(kclient(fake, clock), date(2026, 11, 9)) + "\n" + "\n".join(krx_probe(kclient(fake, clock), date(2026, 11, 9)))
    assert "KOSPI 1건" in text and "TDD_CLSPRC" in text and "빠진 필수 필드 []" in text
    for secret in ("71000", "150000", "98765432100", KEY):
        assert secret not in text


def test_compare_sources_reports_rates_without_values(private_dir):
    clock = Clock(NOW)
    KrxRun(fsc_source(fsc_client(FakeFsc(), clock)), private_dir, now=clock, version="t").run([date(2026, 11, 9)])
    krx = FakeKrx()
    krx.data[("stk_bydd_trd", "20261109")] = [krx_row("005930", "삼성전자", 71000),                 # FSC와 같음
                                              krx_row("000660", "SK하이닉스", 210500)]             # 종가만 다름
    KrxRun(krx_source(kclient(krx, clock)), private_dir, now=clock, version="t").run([date(2026, 11, 9)])
    res = compare_prices.compare(private_dir, "fsc", "krx")
    assert res["days"] == 1 and res["pairs"] == 2 and res["only_krx"] == 1                     # 코스닥 종목은 FSC 가짜에 없음
    assert res["match_rate"]["close"] == 0.5 and res["match_rate"]["volume"] == 1.0
    text = compare_prices.summary(res)
    assert "close" in text and "50.0000%" in text and "71000" not in text and "210500" not in text
    assert compare_prices.short_code("KR7005930003") == "005930" and compare_prices.short_code("A005930") == "005930"
