"""시점 기준 재무 피처: TTM 계산, 성장률, 전년 사업보고서 가용 조건, 종목별 as-of 결합. 값은 꾸민 숫자다."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from feature_store.fundamental_features import fundamentals_at, load_fin_hist, report_table
from orchestration.dart_history import HistoryRun
from tests.unit.test_dart_history import NOW, FakeDart
from tests.unit.test_opendart import client


def ts(s):
    return pd.Timestamp(s, tz="UTC")


def fin_rows():
    base = {"stock_code": "000001", "corp_code": "00000001"}
    return pd.DataFrame([
        {**base, "bsns_year": 2016, "reprt_code": "11011", "bt_available_at": ts("2017-04-02 23:30"),
         "revenue_amt": 1000, "revenue_prev": 900, "net_income_amt": 100, "net_income_prev": 80,
         "equity_amt": 2000, "assets_amt": 5000, "liabilities_amt": 3000},
        {**base, "bsns_year": 2017, "reprt_code": "11013", "bt_available_at": ts("2017-05-16 23:30"),
         "revenue_amt": 300, "revenue_prev": 250, "net_income_amt": 30, "net_income_prev": 20,
         "equity_amt": 2100, "assets_amt": 5100, "liabilities_amt": 3000},
        {**base, "bsns_year": 2017, "reprt_code": "11014", "bt_available_at": ts("2017-11-14 23:30"),
         "revenue_amt": 310, "revenue_cum": 900, "revenue_prev_cum": 800, "net_income_amt": 25, "net_income_cum": 85,
         "net_income_prev_cum": 70, "equity_amt": 2200, "assets_amt": 5300, "liabilities_amt": 3100},
        # 전년 사업보고서가 아직 없는 회사의 분기 → TTM 없음
        {"stock_code": "000002", "corp_code": "00000002", "bsns_year": 2017, "reprt_code": "11013",
         "bt_available_at": ts("2017-05-16 23:30"), "revenue_amt": 50, "revenue_prev": 40, "equity_amt": 500,
         "assets_amt": 800, "liabilities_amt": 300}])


def test_report_table_ttm_and_growth():
    r = report_table(fin_rows()).set_index(["stock_code", "reprt_code"])
    assert r.loc[("000001", "11011"), "revenue_ttm"] == 1000
    assert r.loc[("000001", "11011"), "revenue_growth"] == pytest.approx(100 / 900)
    assert r.loc[("000001", "11013"), "revenue_ttm"] == 1000 + 300 - 250              # 1분기: 3개월 = 누적
    assert r.loc[("000001", "11013"), "revenue_growth"] == pytest.approx(0.2)
    assert r.loc[("000001", "11014"), "revenue_ttm"] == 1000 + 900 - 800
    assert r.loc[("000001", "11014"), "net_income_ttm"] == 100 + 85 - 70
    assert np.isnan(r.loc[("000002", "11013"), "revenue_ttm"])                       # 전년 사업보고서 없음


def test_fundamentals_at_point_in_time():
    reports = report_table(fin_rows())
    feats = pd.DataFrame({"date": pd.to_datetime(["2017-04-10", "2017-06-01", "2017-03-02", "2017-06-01"]),
                          "code": ["000001", "000001", "000001", "000003"],
                          "decision_time_utc": [ts("2017-04-09 23:30"), ts("2017-05-31 23:30"), ts("2017-03-01 23:30"),
                                                ts("2017-05-31 23:30")],
                          "log_mktcap": np.log([10_000, 20_000, 10_000, 1_000])})
    out = fundamentals_at(feats, reports)
    assert out.loc[0, "bm"] == pytest.approx(2000 / 10_000)                          # 사업보고서만 쓸 수 있음
    assert out.loc[0, "sp"] == pytest.approx(1000 / 10_000) and out.loc[0, "fin_age"] == 7
    assert out.loc[1, "bm"] == pytest.approx(2100 / 20_000)                          # 1분기 보고서 이후
    assert out.loc[1, "sp"] == pytest.approx(1050 / 20_000) and out.loc[1, "leverage"] == pytest.approx(3000 / 5100)
    assert out.loc[2, ["bm", "ep", "roe"]].isna().all()                              # 보고서가 나오기 전
    assert out.loc[3, ["bm", "fin_age"]].isna().all()                                # 재무가 없는 종목
    assert list(out["code"]) == list(feats["code"])                                  # 순서 유지


def test_load_fin_hist_from_history_store(tmp_path):
    HistoryRun(client(FakeDart(), max_calls=200), tmp_path, now=lambda: NOW, version="t", log=lambda s: None).run(
        date(2017, 1, 1), date(2017, 6, 30))
    f = load_fin_hist(tmp_path)
    assert len(f) == 1 and f.loc[0, "stock_code"] == "000001" and f.loc[0, "revenue_amt"] == 500
    assert f.loc[0, "bt_available_at"].year == 2017                                   # 백테스트는 공급자 시각(정정일 기준)


def test_experiment_runs_with_fundamentals(tmp_path, monkeypatch):
    from evaluation import run_experiment
    from storage.visibility import PRIVATE_MARKER
    from tests.unit.test_walkforward import synth_panel
    pub, priv = tmp_path / "pub", tmp_path / "priv"
    priv.mkdir()
    (priv / PRIVATE_MARKER).write_text("private\n")
    HistoryRun(client(FakeDart(), max_calls=200), pub, now=lambda: NOW, version="t", log=lambda s: None).run(
        date(2017, 1, 1), date(2017, 6, 30))
    panel = synth_panel(n_codes=40)
    monkeypatch.setattr(run_experiment, "load_price_panel",
                        lambda d, s, e, **kw: panel[(panel["date"] >= pd.Timestamp(s)) & (panel["date"] <= pd.Timestamp(e))])
    out = run_experiment.run(priv, "RET_21D", start=date(2017, 1, 1), first_test=date(2019, 1, 1),
                             vault_start=date(2020, 7, 1), every=21, liquidity_pct=0.0, min_history=126,
                             models=("uncond", "ridge", "gbm"), public_dir=pub, log=lambda s: None)
    assert out["feature_schema_version"] == "price_v1+fund_v1" and "bm" in out["features"]
    cov = out["fundamental_coverage"]
    # 40종목 중 1종목(000001)만 재무가 있고, 2017-05-10 기재정정 다음 세션부터 쓸 수 있다
    assert 0 < cov[2017] <= 0.025 and cov[2018] == pytest.approx(1 / 40, abs=0.001)


def test_unit_errors_do_not_leak_into_ttm_or_market_ratios():
    rows = fin_rows()
    rows.loc[0, ["revenue_amt", "net_income_amt", "equity_amt", "assets_amt", "liabilities_amt"]] *= 1000   # 천 배 단위 오류
    r = report_table(rows).set_index(["stock_code", "reprt_code"])
    assert r.loc[("000001", "11011"), "revenue_ttm"] == 1_000_000                     # 그 보고서만 보면 그대로
    assert np.isnan(r.loc[("000001", "11013"), "revenue_ttm"])                       # 단위가 다른 전년 보고서와 섞지 않음
    assert r.loc[("000001", "11013"), "revenue_growth"] == pytest.approx(0.2)        # 보고서 안 비율은 그대로
    feats = pd.DataFrame({"date": pd.to_datetime(["2017-04-10"]), "code": ["000001"],
                          "decision_time_utc": [ts("2017-04-09 23:30")], "log_mktcap": np.log([5_000])})
    out = fundamentals_at(feats, report_table(rows))
    assert out.loc[0, ["bm", "ep", "sp"]].isna().all() and bool(out.loc[0, "fin_unit_suspect"])   # 자산/시총 1,000배
    assert out.loc[0, "leverage"] == pytest.approx(0.6) and out.loc[0, "fin_age"] == 7
