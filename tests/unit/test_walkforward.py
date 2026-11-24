"""시점 기준 피처, Purged Walk-forward, Vault 차단, 기준선·Model A, 실험 기록. 합성 데이터로 끝까지 돌린다."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from core.calendar_align import kr_decision_cutoff
from evaluation import run_experiment
from evaluation.experiment_registry import ExperimentRegistry
from evaluation.metrics import evaluate, rank_ic
from evaluation.walkforward import assert_no_vault, make_folds, split
from feature_store.price_features import decision_grid, features_at, row_features
from orchestration.krx_daily import sessions_between
from storage.visibility import PRIVATE_MARKER


def synth_panel(n_codes=120, start=date(2017, 1, 2), end=date(2020, 12, 30), seed=0):
    """종목마다 지속되는 기대수익(드리프트)을 심는다 → 과거 수익률(모멘텀)이 미래 수익률을 예측한다."""
    rng = np.random.default_rng(seed)
    days = [pd.Timestamp(d) for d in sessions_between(start, end)]
    cut = [kr_decision_cutoff(days[min(i + 2, len(days) - 1)]) for i in range(len(days))]
    rows = []
    for c in range(n_codes):
        drift = rng.normal(0, 0.0015)
        close = 10_000.0
        for i, d in enumerate(days):
            base = close
            r_open = rng.normal(drift / 2, 0.003)
            r_close = drift + rng.normal(0, 0.02)
            o, cl = base * np.exp(r_open), base * np.exp(r_close)
            vol = int(rng.integers(1_000, 100_000))
            rows.append((d, f"{c:06d}", o, max(o, cl), min(o, cl), cl, vol, vol * cl, 1e6, 1e6 * cl, base, cut[i]))
            close = cl
    p = pd.DataFrame(rows, columns=["date", "code", "open", "high", "low", "close", "volume", "value", "shares",
                                    "mktcap", "base", "bt_available_at"])
    p["name"], p["market"], p["chg"] = p["code"], "KOSPI", p["close"] - p["base"]
    p["factor"] = p["base"] / p.groupby("code")["close"].shift(1)
    p["ret_cc"] = p["close"] / p["base"] - 1
    return p


def test_features_are_point_in_time():
    p = synth_panel(n_codes=3, end=date(2017, 6, 30))
    g = decision_grid(p, every=1)
    f = features_at(p, g)
    assert (f["bt_available_at"].dropna() <= f.loc[f["bt_available_at"].notna(), "decision_time_utc"]).all()
    # 지금 가용 시각 규칙이면 D에 쓰는 마지막 행은 D−2 세션
    days = sorted(p["date"].unique())
    row = f[(f["code"] == "000000") & (f["date"] == days[10])].iloc[0]
    assert row["feature_date"] == days[8]
    rf = row_features(p)
    assert rf.loc[rf["code"] == "000000", "mom_21"].iloc[:20].isna().all()      # 창이 차기 전엔 비움


def test_folds_purge_embargo_and_vault():
    p = synth_panel(n_codes=2)
    days = sorted(p["date"].unique())
    folds = make_folds(days, first_test=date(2019, 1, 1), vault_start=date(2020, 7, 1), test_months=6)
    assert [f.test_start.date() for f in folds] == [date(2019, 1, 2), date(2019, 7, 1), date(2020, 1, 2)]
    assert all(f.test_end < pd.Timestamp("2020-07-01") for f in folds)
    df = pd.DataFrame({"date": days, "decision_time_utc": [kr_decision_cutoff(d) for d in days]})
    df["label_available_at"] = df["decision_time_utc"].shift(-7)                 # 5세션 보유 + 가용 지연
    tr, te = split(df, folds[0])
    assert pd.to_datetime(tr["label_available_at"]).max() < te["decision_time_utc"].min()
    assert len(df[df["date"] < folds[0].test_start]) - len(tr) >= 5                # purge + embargo로 빠진 표본
    with pytest.raises(PermissionError, match="Vault"):
        assert_no_vault(df, date(2020, 7, 1))


def test_metrics_rank_ic_and_spread():
    rng = np.random.default_rng(1)
    rows = []
    for d in pd.date_range("2020-01-01", periods=30):
        s = rng.normal(size=100)
        rows += [{"date": d, "score": a, "ret": 0.5 * a + rng.normal(), "tradable": True, "prob": 0.6} for a in s]
    df = pd.DataFrame(rows)
    r = evaluate(df, horizon=5, step=5, prob="prob")
    assert 0.3 < r["ic_mean"] < 0.6 and r["ic_t_nw"] > 5 and r["top_bottom_decile"] > 0
    assert rank_ic(df.assign(score=1.0)).isna().all()                              # 점수가 한 값이면 IC 없음


def test_end_to_end_finds_planted_signal_and_never_reads_vault(tmp_path, monkeypatch):
    (tmp_path / PRIVATE_MARKER).write_text("private\n")
    panel = synth_panel()
    seen = {}

    def fake_load(data_dir, start, end, **kw):
        seen["end"] = end
        return panel[(panel["date"] >= pd.Timestamp(start)) & (panel["date"] <= pd.Timestamp(end))]
    monkeypatch.setattr(run_experiment, "load_price_panel", fake_load)
    out = run_experiment.run(tmp_path, "RET_21D", start=date(2017, 1, 1), first_test=date(2019, 1, 1),
                             vault_start=date(2020, 7, 1), every=5, liquidity_pct=0.0, min_history=126, log=lambda s: None)
    assert seen["end"] == date(2020, 6, 30)                                      # Vault 시세는 읽지 않음
    m = out["metrics"]
    assert m["momentum"]["ic_mean"] > 0.05 and m["ridge"]["ic_mean"] > 0.05 and m["gbm"]["ic_mean"] > 0.03
    assert np.isnan(m["uncond"]["ic_mean"])                                      # 상수 예측은 순위 정보 없음
    assert 0 < m["gbm"]["brier"] < 0.3
    rec = ExperimentRegistry(tmp_path).all()
    assert len(rec) == 1 and rec[0]["target_definition_id"] == "RET_21D" and rec[0]["experiment_id"] == out["experiment_id"]
    text = run_experiment.report(out)
    assert "한국거래소 통계정보" in text and "gbm" in text and "10000" not in text
    assert out["portfolio"]["results"] == {}                                    # RET_21D를 5세션마다: 보유가 겹쳐 평가 안 함
    assert set(out["ic_by_bucket"]["gbm"]) == {"liq_q", "market", "trend", "vol_regime"}


def test_market_state_is_per_date_and_used_raw(tmp_path, monkeypatch):
    from feature_store.price_features import MARKET_FEATURES, market_state
    f = pd.DataFrame({"date": pd.to_datetime(["2020-01-02"] * 3 + ["2020-01-03"] * 2), "code": list("abcab"),
                      "mom_21": [0.1, -0.1, 0.3, -0.2, -0.4], "mom_63": [0.0] * 5, "vol_21": [0.02] * 5})
    m = market_state(f)
    assert m.loc[0, "mkt_mom_21"] == pytest.approx(0.1) and m.loc[0, "mkt_breadth_21"] == pytest.approx(2 / 3)
    assert m.loc[3, "mkt_breadth_21"] == 0.0 and m["mkt_mom_21"].nunique() == 2         # 판단일마다 같은 값
    (tmp_path / PRIVATE_MARKER).write_text("private\n")
    panel = synth_panel(n_codes=120)
    monkeypatch.setattr(run_experiment, "load_price_panel",
                        lambda d, s, e, **kw: panel[(panel["date"] >= pd.Timestamp(s)) & (panel["date"] <= pd.Timestamp(e))])
    out = run_experiment.run(tmp_path, "RET_21D", start=date(2017, 1, 1), first_test=date(2019, 1, 1),
                             vault_start=date(2020, 7, 1), every=5, liquidity_pct=0.0, min_history=126,
                             models=("uncond", "gbm"), market=True, log=lambda s: None)
    assert out["feature_schema_version"] == "price_v1+mkt_v1" and out["raw_features"] == list(MARKET_FEATURES)
    assert out["metrics"]["gbm"]["ic_mean"] > 0.05                                   # 심어 둔 신호는 그대로 찾는다


def test_price_v2_features_and_rank_ensemble_models(tmp_path, monkeypatch):
    from feature_store.price_features import FEATURES_V2
    p = synth_panel(n_codes=4, end=date(2017, 9, 29))
    rf = row_features(p)
    a = rf[rf["code"] == "000001"].reset_index(drop=True)
    assert a["max_ret_21"].iloc[:20].isna().all() and a["skew_63"].iloc[:62].isna().all()          # 창이 차기 전엔 비움
    assert a.loc[30, "max_ret_21"] == pytest.approx(a["rev_1"].iloc[10:31].max())                  # 그날까지의 21세션
    assert np.isfinite(a.loc[100, ["amihud_21", "ivol_63"]].astype(float)).all()
    # 미래 행을 바꿔도 과거 피처는 그대로 (과거 방향 창만)
    cut = sorted(p["date"].unique())[150]
    q = p.copy()
    q.loc[q["date"] > cut, "close"] *= 1.5
    b = row_features(q)
    pd.testing.assert_frame_equal(rf[rf["date"] <= cut][list(FEATURES_V2)].reset_index(drop=True),
                                  b[b["date"] <= cut][list(FEATURES_V2)].reset_index(drop=True))
    (tmp_path / PRIVATE_MARKER).write_text("private\n")
    panel = synth_panel(n_codes=120)
    monkeypatch.setattr(run_experiment, "load_price_panel",
                        lambda d, s, e, **kw: panel[(panel["date"] >= pd.Timestamp(s)) & (panel["date"] <= pd.Timestamp(e))])
    out = run_experiment.run(tmp_path, "RET_21D", start=date(2017, 1, 1), first_test=date(2019, 7, 1),
                             vault_start=date(2020, 7, 1), every=5, liquidity_pct=0.0, min_history=126,
                             models=("ridge_rank", "gbm_rank", "ens"), feature_set="v2", train_start=date(2018, 1, 1),
                             log=lambda s: None)
    assert out["feature_schema_version"] == "price_v2" and "ivol_63" in out["features"] and out["train_start"] == "2018-01-01"
    assert all(out["metrics"][m]["ic_mean"] > 0.03 for m in ("ridge_rank", "gbm_rank", "ens"))     # 심어 둔 신호를 찾는다


def test_mlp_and_mlp_ensembles_find_planted_signal(tmp_path, monkeypatch):
    (tmp_path / PRIVATE_MARKER).write_text("private\n")
    panel = synth_panel(n_codes=120)
    monkeypatch.setattr(run_experiment, "load_price_panel",
                        lambda d, s, e, **kw: panel[(panel["date"] >= pd.Timestamp(s)) & (panel["date"] <= pd.Timestamp(e))])
    out = run_experiment.run(tmp_path, "RET_21D", start=date(2017, 1, 1), first_test=date(2019, 7, 1),
                             vault_start=date(2020, 7, 1), every=5, liquidity_pct=0.0, min_history=126,
                             models=("mlp", "ens_gm", "ens3"), log=lambda s: None)
    assert all(out["metrics"][m]["ic_mean"] > 0.03 for m in ("mlp", "ens_gm", "ens3"))
    assert out["hyperparameters"]["mlp"]["hidden_layer_sizes"] == (32, 16)
    assert set(out["hyperparameters"]["ens3"]) == {"ridge", "gbm", "mlp"}
