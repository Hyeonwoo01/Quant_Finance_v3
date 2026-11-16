"""비용 모델, Long-only 상위 분위 포트폴리오, 상장폐지 수익률, 시점 기준 국면 구분. 값은 꾸민 숫자다."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from evaluation.costs import SCENARIOS, CostScenario, sell_tax, slippage
from evaluation.portfolio_eval import long_only, summarize
from evaluation.run_experiment import add_buckets
from evaluation.targets import build_targets
from tests.unit.test_targets_audit import make_panel


def test_sell_tax_schedule():
    t = sell_tax([date(2015, 1, 2), date(2019, 6, 3), date(2021, 1, 4), date(2024, 1, 2), date(2025, 6, 2), date(2026, 1, 2)])
    assert list(t) == [0.0030, 0.0025, 0.0023, 0.0018, 0.0015, 0.0020]


def test_slippage_grows_for_cheap_stocks_and_scenarios():
    base, stress = SCENARIOS[0], SCENARIOS[2]
    s = slippage([1_500, 150_000], [date(2024, 1, 2)] * 2, ["KOSDAQ", "KOSPI"], base)
    assert s[0] == pytest.approx(1 / 1_500) and s[1] == pytest.approx(100 / 150_000)
    assert slippage([1_500], [date(2024, 1, 2)], ["KOSDAQ"], stress)[0] == pytest.approx(3 / 1_500 + 15e-4)


def pred_frame():
    """판단일 2개 × 30종목. 점수 상위 3개(10%)를 산다."""
    rows = []
    for d, codes_up in ((pd.Timestamp("2024-01-02"), ("c29", "c28", "c27")), (pd.Timestamp("2024-01-09"), ("c29", "c28", "c26"))):
        for i in range(30):
            c = f"c{i:02d}"
            rows.append({"date": d, "code": c, "score": float(i if d.day == 2 else (i if c != "c27" else -1)),
                         "ret": 0.02 if c in codes_up else 0.0, "entry_open": 10_000.0, "market": "KOSPI",
                         "entry_no_trade": False, "entry_limit_up": False, "exit_no_trade": False, "exit_limit_down": False,
                         "no_exit_row": False, "ret_delist": np.nan, "log_adv_21": np.log(1e12)})
    return pd.DataFrame(rows)


def test_long_only_costs_only_on_changes():
    p = pred_frame()
    sc = CostScenario("t", commission=0.0, slippage_ticks=0.0, extra_bps=0.0, max_participation=1.0)
    res = long_only(p, h=5, scenario=sc)
    assert list(res["n"]) == [3, 3] and res["gross"].tolist() == pytest.approx([0.02, 0.02])
    # 첫 판단일은 전부 매수(세금 없음), 둘째 판단일은 c27을 팔고 c26을 산다 → 매도분 1/3에만 세금
    assert res.loc[0, "cost"] == pytest.approx(0.0)
    assert res.loc[1, "cost"] == pytest.approx(1 / 3 * float(sell_tax([date(2024, 1, 9)])[0]), rel=0.05)
    assert res.loc[1, "turnover"] == pytest.approx(1 / 3, rel=0.05)
    assert res["bench"].tolist() == pytest.approx([0.002, 0.002])


def test_long_only_unfilled_capacity_and_delisting():
    p = pred_frame()
    first = p["date"] == p["date"].min()
    p.loc[first & (p["code"] == "c29"), "entry_limit_up"] = True              # 상한가 → 체결 안 됨, 현금
    p.loc[first & (p["code"] == "c28"), "log_adv_21"] = np.log(1e8)           # 거래대금이 작아 일부만 체결
    p.loc[first & (p["code"] == "c27"), ["no_exit_row", "ret", "ret_delist"]] = [True, np.nan, -0.5]
    sc = CostScenario("t", commission=0.0, slippage_ticks=0.0, max_participation=0.05)
    r = long_only(p, h=5, notional=1e8, scenario=sc).iloc[0]
    # c28: 주문 1억/3 중 거래대금 1억 × 5% = 500만만 체결 → 0.15 비율
    assert r["cash"] == pytest.approx(1 - (0 + 0.15 + 1) / 3, rel=1e-6)
    assert r["gross"] == pytest.approx((0.15 * 0.02 + 1 * -0.5) / 3, rel=1e-6) and r["delisted"] == 1
    p.loc[first & (p["code"] == "c27"), "ret_delist"] = np.nan                 # 마지막 종가도 없으면 −100%
    assert long_only(p, h=5, notional=1e8, scenario=sc).iloc[0]["gross"] == pytest.approx((0.15 * 0.02 - 1) / 3, rel=1e-6)
    s = summarize(long_only(pred_frame(), h=5), h=5)
    assert s["periods"] == 2 and "excess_by_year" in s and s["ann_net"] > s["ann_bench"]


def test_delisted_return_uses_last_close():
    panel, days = make_panel()
    t = build_targets(panel, horizons=(5,))
    c = t[(t.code == "C") & t.no_exit_row].iloc[0]
    last = panel[panel.code == "C"].iloc[-1]
    assert c["ret_delist"] == pytest.approx(last["close"] / c["entry_open"] - 1, rel=1e-9)   # C는 기업행위 없음
    assert t.loc[~t.no_exit_row, "ret_delist"].isna().all()


def test_regime_buckets_use_only_past():
    rng = np.random.default_rng(0)
    rows = [{"date": d, "code": f"c{i}", "log_adv_21": rng.normal(), "mom_63": m, "vol_21": v}
            for d, m, v in zip(pd.date_range("2020-01-01", periods=4), (0.1, -0.1, 0.1, 0.1), (0.02, 0.03, 0.01, 0.05))
            for i in range(10)]
    df = pd.DataFrame(rows)
    add_buckets(df)
    by = df.groupby("date").first()
    assert list(by["trend"]) == ["up", "down", "up", "up"]
    assert list(by["vol_regime"]) == ["n/a", "high", "low", "high"]       # 첫날은 비교할 과거가 없음
    assert set(df["liq_q"]) == {1, 2, 3, 4, 5}


def test_keep_buffer_reduces_turnover():
    p = pred_frame()
    second = p["date"] == p["date"].max()
    p.loc[second & (p["code"] == "c27"), "score"] = 25.5        # 둘째 판단일: 상위 10% 밖, 상위 20% 안
    sc = CostScenario("t", commission=0.0, slippage_ticks=0.0, max_participation=1.0)
    plain = long_only(p, h=5, scenario=sc)
    buffered = long_only(p, h=5, keep=0.2, scenario=sc)
    assert plain.loc[1, "turnover"] > 0.3 and buffered.loc[1, "n"] == 4      # c27을 계속 들고 c26을 더 산다
    assert buffered.loc[1, "turnover"] < plain.loc[1, "turnover"]
