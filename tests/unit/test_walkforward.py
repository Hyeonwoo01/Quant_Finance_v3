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
