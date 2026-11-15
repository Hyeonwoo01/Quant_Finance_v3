"""호가단위·가격제한폭, 예측 타깃(시가 진입·시가 청산), 체결 가능성, 예측 기록 정산과 회고. 값은 꾸민 숫자다."""
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from core.calendar_align import kr_decision_cutoff
from core.data_contracts import PermissionDenied
from evaluation.forecast_audit import ForecastLedger, ForecastRecord, attribute, session_start_after
from evaluation.kr_market_rules import BASE_TICK, DOUBLE, LIMIT_TICK, check_against_data, limit_prices, tick_size
from evaluation.targets import TARGETS, build_targets, summarize
from orchestration.krx_daily import sessions_between
from storage.visibility import PRIVATE_MARKER

UTC = timezone.utc


def test_tick_tables_change_on_reform_day():
    assert tick_size(1_500, date(2023, 1, 20), "KOSPI") == 5 and tick_size(1_500, date(2023, 1, 25), "KOSPI") == 1
    assert tick_size(60_000, date(2022, 12, 1), "KOSDAQ") == 100 and tick_size(60_000, date(2022, 12, 1), "KOSPI") == 100
    assert tick_size(600_000, date(2022, 12, 1), "KOSDAQ") == 100 and tick_size(600_000, date(2022, 12, 1), "KOSPI") == 1_000
    assert tick_size(15_000, date(2024, 1, 2), "KOSDAQ") == 10


def test_limit_prices_methods_and_regimes():
    assert limit_prices(10_000, date(2014, 1, 2), "KOSPI") == (8_500, 11_500)              # ±15% 시절
    assert limit_prices(10_000, date(2015, 6, 15), "KOSPI") == (7_000, 13_000)             # ±30%부터
    # 가격대 경계: 기준가 호가단위로만 자르면 주문할 수 없는 가격(12,970)이 나온다
    assert limit_prices(9_980, date(2020, 1, 2), "KOSPI", BASE_TICK) == (6_990, 12_970)
    assert limit_prices(9_980, date(2020, 1, 2), "KOSPI", DOUBLE) == (6_990, 12_950)
    assert limit_prices(9_980, date(2020, 1, 2), "KOSPI", LIMIT_TICK) == (6_990, 12_950)
    assert limit_prices(None, date(2020, 1, 2), "KOSPI") is None and limit_prices(0, date(2020, 1, 2), "KOSPI") is None


def make_panel():
    """KRX 세션 30일, 종목 3개.
    A: 매일 1% 상승, 10번째 날 1:2 분할(기준가 절반). B: 12번째 날 거래정지, 15번째 날 시가가 상한가.
    C: 20번째 날 뒤 상장폐지 (행 없음)."""
    days = [pd.Timestamp(d) for d in sessions_between(date(2024, 3, 4), date(2024, 4, 30))][:30]
    rows = []
    for code, start in (("A", 10_000), ("B", 20_000), ("C", 5_000)):
        close = start
        for i, d in enumerate(days):
            if code == "C" and i > 20:
                break
            base = close
            if code == "A" and i == 10:
                base = close / 2                                            # 1:2 분할
            o = round(base * 1.01)
            c = round(base * 1.01)
            vol = 1000
            if code == "B" and i == 12:
                o, c, vol = 0, base, 0                                      # 거래정지: 시가 0, 종가 = 기준가
            if code == "B" and i == 15:
                o = c = limit_prices(base, d.date(), "KOSPI")[1]          # 상한가 시가
            rows.append({"date": d, "code": code, "name": code, "market": "KOSPI", "open": o, "high": max(o, c),
                         "low": min(o, c) if o else c, "close": c, "volume": vol, "value": vol * c, "shares": 1,
                         "mktcap": c, "base": base, "chg": c - base,
                         "bt_available_at": kr_decision_cutoff(days[min(i + 2, 29)])})
            close = c
    p = pd.DataFrame(rows).sort_values(["code", "date"], ignore_index=True)
    prev = p.groupby("code")["close"].shift(1)
    p["factor"] = p["base"] / prev
    p["ret_cc"] = p["close"] / p["base"] - 1
    return p, days


def test_targets_open_to_open_adjusted_and_flags():
    panel, days = make_panel()
    t = build_targets(panel, horizons=(1, 5))
    a = t[(t.code == "A") & (t.h == 5)].set_index("date")
    # 분할을 지나도 수익률은 실제 상승(매일 1%)만 남는다: 시가→시가 5세션 = 1.01^5 − 1
    assert a.loc[days[7], "ret"] == pytest.approx(1.01 ** 5 - 1, rel=1e-3)
    assert bool(a.loc[days[7], "action_in_window"]) and not bool(a.loc[days[0], "action_in_window"])
    b = t[(t.code == "B") & (t.h == 1)].set_index("date")
    assert bool(b.loc[days[12], "entry_no_trade"]) and np.isnan(b.loc[days[12], "ret"])
    assert bool(b.loc[days[11], "exit_no_trade"])
    assert bool(b.loc[days[15], "entry_limit_up"]) and not bool(b.loc[days[15], "tradable"])
    c = t[(t.code == "C") & (t.h == 5)].set_index("date")
    assert bool(c.loc[days[18], "no_exit_row"]) and np.isnan(c.loc[days[18], "ret"])   # 버리지 않고 사유를 남김
    assert days[29] not in set(t[t.h == 1]["date"])                                     # 청산 세션이 없는 날은 없음
    s = summarize(t)
    assert s[1]["entry_no_trade"] >= 1 and s[5]["no_exit_row"] >= 1 and set(TARGETS) >= {"RET_1D", "DIR_21D"}


def test_limit_check_counts_without_values():
    panel, _ = make_panel()
    res = check_against_data(panel)
    assert res[DOUBLE]["over"] == 0 and res[DOUBLE]["exact_limit_close"] == 1


@pytest.fixture
def ledger(tmp_path):
    (tmp_path / PRIVATE_MARKER).write_text("private\n")
    return ForecastLedger(tmp_path)


def rec(fid, d, created, **kw):
    cut = kr_decision_cutoff(d).to_pydatetime()
    return ForecastRecord(forecast_id=fid, created_at_utc=created, decision_time_utc=cut, data_cutoff_utc=cut,
                          ticker=kw.pop("ticker", "A"), model_id="m0", target_definition_id="RET_5D",
                          holding_period_sessions=5, **kw)


def test_no_backfilled_forecasts(ledger, tmp_path):
    d = date(2024, 3, 11)
    cut = kr_decision_cutoff(d).to_pydatetime()
    assert session_start_after(cut) == datetime(2024, 3, 11, 0, 0, tzinfo=UTC)          # 같은 날 09:00 KST
    ledger.record(rec("f1", d, cut + timedelta(minutes=5), predicted_return=0.01))
    with pytest.raises(ValueError, match="소급"):
        ledger.record(rec("f2", d, cut + timedelta(hours=1), predicted_return=0.01))       # 09:30 KST에 만든 예측
    with pytest.raises(ValueError, match="고치지"):
        ledger.record(rec("f1", d, cut, predicted_return=0.02))
    with pytest.raises(PermissionDenied):
        ForecastLedger(tmp_path / "public").record(rec("f3", d, cut, predicted_return=0.01))


def test_settle_and_attribution(ledger):
    panel, days = make_panel()
    t = build_targets(panel, horizons=(5,))
    d_ok, d_lim = days[7].date(), days[15].date()
    for fid, d, kw in (("up", d_ok, {"predicted_return": 0.02}),
                       ("down", d_ok, {"predicted_return": -0.03, "predicted_probability": 0.2, "ticker": "C"}),
                       ("lim", d_lim, {"predicted_return": 0.01, "ticker": "B"}),
                       ("late", days[26].date(), {"predicted_return": 0.01})):
        cut = kr_decision_cutoff(d).to_pydatetime()
        ledger.record(rec(fid, d, cut, **kw))
    now = datetime(2024, 6, 1, tzinfo=UTC)
    evs = {e.forecast_id: e for e in ledger.settle(t, now)}
    assert set(evs) == {"up", "down", "lim"}                         # late는 청산 세션이 자료 밖이라 아직 PENDING
    assert evs["up"].fill_status == "FILLED" and evs["up"].return_gross == pytest.approx(1.01 ** 5 - 1, rel=1e-3)
    assert evs["up"].failure_attribution is None                     # 맞힌 예측
    assert evs["lim"].fill_status == "NOT_FILLED" and evs["lim"].return_gross is None and evs["lim"].realized_return is not None
    att = evs["down"].failure_attribution                            # 하락 확률 80%로 냈는데 상승
    assert att["overconfident"] and "확률 과대평가" in att["candidates"] and "인과를 확정하지 않는다" in att["note"]
    assert {p.forecast_id for p in ledger.pending()} == {"late"} and ledger.settle(t, now) == []


def test_attribute_market_vs_idiosyncratic():
    f = rec("x", date(2024, 3, 11), kr_decision_cutoff(date(2024, 3, 11)).to_pydatetime(), predicted_return=0.02)
    row = {"entry_limit_up": False, "exit_limit_down": False, "action_in_window": False, "entry_gap": 0.0}
    a = attribute(f, -0.08, -0.07, row)
    assert "시장 전체 움직임" in a["candidates"] and "종목 고유 움직임 (시장 대비)" not in a["candidates"]
    b = attribute(f, -0.08, 0.01, row)
    assert "종목 고유 움직임 (시장 대비)" in b["candidates"] and "시장 전체 움직임" not in b["candidates"]
    assert attribute(f, -0.001, 0.0, row) is None                    # 작게 틀린 것은 회고하지 않음


def test_vectorized_limits_match_scalar():
    from evaluation.kr_market_rules import limit_prices_arr
    rng = np.random.default_rng(0)
    n = 5000
    base = rng.integers(50, 900_000, n).astype(float)
    dates = pd.to_datetime(rng.choice(pd.date_range("2010-01-01", "2026-11-01").values, n))
    mk = rng.choice(["KOSPI", "KOSDAQ"], n)
    for m in (BASE_TICK, LIMIT_TICK, DOUBLE):
        lo, up = limit_prices_arr(base, dates, mk, m)
        ref = [limit_prices(b, d.date(), k, m) for b, d, k in zip(base, dates, mk)]
        assert np.array_equal(lo, [r[0] for r in ref]) and np.array_equal(up, [r[1] for r in ref])
    lo, up = limit_prices_arr([np.nan, 0, 10_000], [date(2020, 1, 2)] * 3, ["KOSPI"] * 3)
    assert np.isnan(lo[0]) and np.isnan(up[1]) and up[2] == 13_000
