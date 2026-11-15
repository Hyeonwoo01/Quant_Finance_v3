"""기준가(수정주가 계수) 시리즈, 원본에서 다시 만들기, 백테스트용 가용 시각, 학습용 시세 패널, 공개 시각 관측.
값은 꾸민 숫자다."""
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from collectors.krx_openapi import SERIES_KRX_BASE, SERIES_KRX_PRICE, base_records, record_license
from core.availability import backtest_available_at
from core.data_contracts import PermissionDenied
from feature_store.price_panel import corporate_action_days, describe, load_price_panel
from orchestration.krx_daily import PUBLICATION_LOG, KrxRun, PriceSource, krx_source, last_closed_session
from storage.pit_store import read_parquet
from tests.unit.test_krx_openapi import NOW, Clock, FakeKrx, kclient, krx_row, private_dir  # noqa: F401

UTC = timezone.utc
D1, D2 = date(2026, 11, 9), date(2026, 11, 10)


def split_fake():
    """D1 종가 50,000 → D2 1:5 액면분할 (기준가 10,000), D2 종가 10,500. 다른 종목은 기업행위 없음."""
    f = FakeKrx()
    f.data[("stk_bydd_trd", "20261109")] = [krx_row("005930", "삼성전자", 50000, CMPPREVDD_PRC="500"),
                                            krx_row("000660", "SK하이닉스", 200000, CMPPREVDD_PRC="-1000")]
    f.data[("stk_bydd_trd", "20261110")] = [
        krx_row("005930", "삼성전자", 10500, day="20261110", CMPPREVDD_PRC="500", LIST_SHRS="29598189610"),
        krx_row("000660", "SK하이닉스", 202000, day="20261110", CMPPREVDD_PRC="2000")]
    f.data[("ksq_bydd_trd", "20261109")] = f.data[("ksq_bydd_trd", "20261110")] = []
    return f


def test_base_records_are_close_minus_change():
    rows = [krx_row("005930", "삼성전자", 10500, day="20261110", CMPPREVDD_PRC="500"),
            krx_row("000660", "SK하이닉스", 100, day="20261110", CMPPREVDD_PRC="-"),       # 전일대비 없음
            krx_row("035720", "카카오", 100, day="20261110", CMPPREVDD_PRC="100")]        # 기준가 0
    recs, counts = base_records(rows, D2, contract_id="krx_openapi_stock_v1", raw_sha="x", ingested_at_utc=NOW,
                                latest=lambda k: None)
    assert [json.loads(r.value) for r in recs] == [{"base": 10000, "chg": 500}] and counts["bad_rows"] == 2
    assert recs[0].series_id == SERIES_KRX_BASE and "|base|" in recs[0].revision_id


def test_collect_also_stores_base_series(private_dir):
    clock = Clock(NOW)
    m = KrxRun(krx_source(kclient(split_fake(), clock)), private_dir, now=clock, version="t").run([D1, D2])
    assert m.status == "ok" and m.counts["krx_openapi_base_added"] == 4
    recs = read_parquet(private_dir / "pit_versions" / SERIES_KRX_BASE / "2026-11-10.parquet")
    assert {r.entity_id: json.loads(r.value)["base"] for r in recs} == {"KRX:005930": 10000, "KRX:000660": 200000}


def test_derive_from_raw_rebuilds_base_for_old_days(private_dir):
    clock = Clock(NOW)
    old = krx_source(kclient(split_fake(), clock))
    no_extra = PriceSource(**{**old.__dict__, "extra": ()})               # 기준가를 저장하지 않던 시절의 수집
    KrxRun(no_extra, private_dir, now=clock, version="t").run([D1, D2])
    assert not (private_dir / "pit_versions" / SERIES_KRX_BASE).exists()
    later = Clock(NOW + timedelta(days=5))
    m = KrxRun(old, private_dir, now=later, version="t").derive_from_raw()
    assert m.status == "ok" and m.counts["krx_openapi_base_added"] == 4 and m.counts["days"] == 2
    rec = read_parquet(private_dir / "pit_versions" / SERIES_KRX_BASE / "2026-11-10.parquet")[0]
    assert rec.ingested_at_utc == NOW                                     # 원본을 실제로 받은 시각
    m2 = KrxRun(old, private_dir, now=later, version="t").derive_from_raw()
    assert m2.counts.get("krx_openapi_base_added", 0) == 0                # 다시 돌려도 그대로


def test_backtest_available_at_only_for_original_market_data(private_dir):
    clock = Clock(datetime(2026, 11, 15, 9, 0, tzinfo=UTC))              # 과거 채우기처럼 늦게 받음
    KrxRun(krx_source(kclient(split_fake(), clock)), private_dir, now=clock, version="t").run([D1])
    rec = read_parquet(private_dir / "pit_versions" / SERIES_KRX_PRICE / "2026-11-09.parquet")[0]
    assert rec.available_at_utc == datetime(2026, 11, 15, 23, 30, tzinfo=UTC)       # 받은 뒤 첫 08:30 (월요일)
    assert backtest_available_at(rec) == datetime(2026, 11, 10, 23, 30, tzinfo=UTC) # 다음 세션 13:00 뒤 첫 08:30
    assert backtest_available_at(replace(rec, supersedes_revision_id="old")) == rec.available_at_utc   # 정정본
    assert backtest_available_at(replace(rec, series_id="dart_fs")) == rec.available_at_utc            # 정정되는 값


def test_panel_adjusts_for_split_and_respects_as_of(private_dir):
    clock = Clock(datetime(2026, 11, 15, 9, 0, tzinfo=UTC))
    KrxRun(krx_source(kclient(split_fake(), clock)), private_dir, now=clock, version="t").run([D1, D2])
    with pytest.raises(PermissionDenied, match="이용기간"):
        load_price_panel(private_dir, D1, D2, today=date(2026, 11, 15))     # 이용기간 기록 없음
    record_license(private_dir, date(2027, 10, 25), NOW)
    panel = load_price_panel(private_dir, D1, D2, today=date(2026, 11, 15))
    s = panel[panel.code == "005930"].set_index("date")
    assert s.loc[pd.Timestamp(D2), "factor"] == pytest.approx(0.2)          # 10,000 / 50,000
    assert s.loc[pd.Timestamp(D2), "ret_cc"] == pytest.approx(0.05)         # 분할을 뺀 실제 상승
    assert pd.isna(s.loc[pd.Timestamp(D1), "factor"])                       # 그 전날 자료 없음
    h = panel[panel.code == "000660"].set_index("date")
    assert h.loc[pd.Timestamp(D2), "factor"] == 1.0
    assert corporate_action_days(panel).to_dict("records") == [{"date": pd.Timestamp(D2), "code": "005930", "factor": 0.2}]
    d = describe(panel)
    assert d["rows"] == 4 and d["action_days"] == 1 and "50000" not in json.dumps(d)
    # as_of: D1 값은 D1 다음 세션 13:00 뒤 첫 08:30(11/10 23:30 UTC)부터, D2 값은 11/11 23:30 UTC부터
    early = load_price_panel(private_dir, D1, D2, today=date(2026, 11, 15), as_of_utc=datetime(2026, 11, 11, 0, tzinfo=UTC))
    assert sorted(early["date"].dt.date.unique()) == [D1]
    # 첫날의 계수: 시작일 전 파일의 종가를 쓴다
    later = load_price_panel(private_dir, D2, D2, today=date(2026, 11, 15))
    assert later.set_index("code").loc["005930", "factor"] == pytest.approx(0.2) and len(later) == 2
    with pytest.raises(PermissionDenied, match="이용기간"):
        load_price_panel(private_dir, D1, D2, today=date(2027, 10, 26))     # 이용기간이 지남


def test_observe_publication_logs_without_storing(private_dir):
    clock = Clock(datetime(2026, 11, 10, 9, 5, tzinfo=UTC))              # 18:05 KST
    assert last_closed_session(clock()) == D2
    assert last_closed_session(datetime(2026, 11, 10, 5, 0, tzinfo=UTC)) == D1   # 14:00 KST: 오늘 장 아직
    rec = KrxRun(krx_source(kclient(split_fake(), clock)), private_dir, now=clock, version="t").observe_publication(D2)
    assert rec["rows"] == {"KOSPI": 2, "KOSDAQ": 0} and rec["available"] is False   # 코스닥이 비어 있으면 아직 아님
    line = json.loads((private_dir / PUBLICATION_LOG).read_text().splitlines()[0])
    assert line["bas_dd"] == "2026-11-10" and line["checked_at_kst"].startswith("2026-11-10T18:05")
    assert not (private_dir / "pit_versions").exists()
