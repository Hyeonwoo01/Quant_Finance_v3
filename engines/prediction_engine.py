"""타깃별 OOS 예측 (가이드 7-2, 7-5, 7-6, 17장: 전체 표본 fit·누출 전처리 금지).

전처리: 판단일마다 횡단면 순위를 [-0.5, 0.5]로 바꾼다 (결측은 0 = 중앙). 판단일 안에서만 계산하므로 다른 시점의
정보가 섞이지 않고, 학습 폴드에서 fit할 것이 없다. 이상치·스케일 차이도 함께 정리된다.

모델 (같은 피처, 같은 폴드):
  uncond     학습 구간 평균 수익률·상승률을 그대로 낸다 (Historical Mean·무조건부 상승률 기준선)
  momentum   mom_252_21 점수 (학습 없음, 단순 모멘텀 기준선)
  reversal   −rev_1 점수 (학습 없음, 단기 반전 기준선)
  ridge      Ridge 회귀 (수익률) + 로지스틱 회귀 (상승 확률) — 단순 선형 기준선
  gbm        HistGradientBoosting 회귀 + 분류 — Model A (가격·거래량 피처 v1)
하이퍼파라미터는 고정값이다. 탐색하면 그 횟수를 실험 기록에 남겨야 한다 (7-9, 7-12).
"""
import numpy as np
import pandas as pd

MODELS = ("uncond", "momentum", "reversal", "ridge", "gbm")
# 2026-11-24 탐색 묶음:
#   ridge_rank·gbm_rank  목표를 수익률 대신 판단일 안 수익률 순위([-0.5, 0.5])로 — 평가 지표(Rank IC)와 맞추고 꼬리에 덜 흔들림
#   ens                  ridge와 gbm 점수를 판단일 안 순위로 바꿔 평균 (두 신호가 다른 종목에서 강함: 저유동 vs 고유동)
EXTRA_MODELS = ("ridge_rank", "gbm_rank", "ens", "mlp", "ens_gm", "ens3")
#   mlp     얕은 신경망 (은닉층 32·16, ReLU). 목표는 판단일 안 수익률 순위 (제곱 오차가 꼬리에 흔들리지 않게).
#           초기값에 따른 흔들림을 줄이려고 시드 3개 평균. 상승 확률은 내지 않는다 (prob = NaN)
#   ens_gm  gbm·mlp 점수 순위 평균 / ens3 = ridge·gbm·mlp 점수 순위 평균 (2026-11-24 탐색 묶음 D)
ENSEMBLES = {"ens": ("ridge", "gbm"), "ens_gm": ("gbm", "mlp"), "ens3": ("ridge", "gbm", "mlp")}
PARAMS = {
    "ridge": {"alpha": 10.0, "logit_C": 0.1},
    "gbm": {"max_iter": 200, "learning_rate": 0.05, "max_leaf_nodes": 31, "min_samples_leaf": 500,
            "l2_regularization": 1.0, "random_state": 0},
    "mlp": {"hidden_layer_sizes": (32, 16), "alpha": 1e-4, "learning_rate_init": 1e-3, "batch_size": 1024,
            "max_iter": 50, "early_stopping": True, "validation_fraction": 0.1, "n_iter_no_change": 5, "seeds": (0, 1, 2)},
}


def cross_sectional_rank(df: pd.DataFrame, cols, by: str = "date") -> pd.DataFrame:
    out = df[cols].groupby(df[by]).rank(pct=True) - 0.5
    return out.fillna(0.0)


def fit_predict(model: str, train: pd.DataFrame, test: pd.DataFrame, features, target: str = "ret",
                raw_features=()) -> pd.DataFrame:
    """test에 score(클수록 수익률이 높을 것)와 prob(상승 확률, 없으면 NaN)를 붙여 돌려준다.

    raw_features: 판단일마다 모든 종목에 같은 값(시장 상태 등). 순위로 바꾸면 0이 되므로 gbm에만 원값으로 넣어
    종목 피처와의 상호작용을 배우게 한다 (트리는 크기 조정이 필요 없어 학습 폴드에서 fit할 것이 없다)."""
    out = test[["date", "code"]].copy()
    if model in ENSEMBLES:
        return combine([fit_predict(m, train, test, features, target, raw_features) for m in ENSEMBLES[model]])
    if model == "mlp":
        return _mlp(train, test, features, target, out)
    tr = train.dropna(subset=[target])
    up = (tr[target].to_numpy() > 0).astype(int)
    if model.endswith("_rank"):                           # 목표 = 판단일 안 수익률 순위
        y = (tr[target].groupby(tr["date"]).rank(pct=True) - 0.5).to_numpy()
        model = model[: -len("_rank")]
        rank_target = True
    else:
        y = tr[target].to_numpy()
        rank_target = False
    if model == "uncond":
        out["score"] = float(np.mean(y)) if len(y) else 0.0
        out["prob"] = float(up.mean()) if len(up) else 0.5
        return out
    if model == "momentum":
        out["score"], out["prob"] = test["mom_252_21"].to_numpy(), np.nan
        return out
    if model == "reversal":
        out["score"], out["prob"] = -test["rev_1"].to_numpy(), np.nan
        return out
    Xtr = cross_sectional_rank(tr, list(features)).to_numpy()
    Xte = cross_sectional_rank(test, list(features)).to_numpy()
    if model == "ridge":
        from sklearn.linear_model import LogisticRegression, Ridge
        reg = Ridge(alpha=PARAMS["ridge"]["alpha"]).fit(Xtr, y)
        clf = LogisticRegression(C=PARAMS["ridge"]["logit_C"], max_iter=500).fit(Xtr, up)
    elif model == "gbm":
        from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
        p = PARAMS["gbm"]
        if raw_features:
            Xtr = np.hstack([Xtr, tr[list(raw_features)].to_numpy(dtype=float)])
            Xte = np.hstack([Xte, test[list(raw_features)].to_numpy(dtype=float)])
        loss = "squared_error" if rank_target else "absolute_error"        # 순위 목표는 꼬리가 없다
        reg = HistGradientBoostingRegressor(loss=loss, **p).fit(Xtr, y)   # 수익률 목표는 꼬리가 두꺼워 절대 오차
        clf = HistGradientBoostingClassifier(**p).fit(Xtr, up)
    else:
        raise ValueError(f"알 수 없는 모델: {model}")
    out["score"] = reg.predict(Xte)
    out["prob"] = clf.predict_proba(Xte)[:, 1]
    return out


def combine(parts) -> pd.DataFrame:
    """구성 모델 예측(같은 test 행 순서)들의 점수를 판단일 안 순위로 바꿔 평균. 확률은 있는 것만 평균."""
    out = parts[0][["date", "code"]].copy()
    ranks = np.column_stack([q["score"].groupby(q["date"]).rank(pct=True).to_numpy() for q in parts])
    probs = np.column_stack([q["prob"].to_numpy(dtype=float) for q in parts])
    out["score"] = ranks.mean(axis=1)
    if np.isfinite(probs).any():
        n = np.isfinite(probs).sum(axis=1)
        out["prob"] = np.where(n > 0, np.nansum(probs, axis=1) / np.maximum(n, 1), np.nan)
    else:
        out["prob"] = np.nan
    return out


def _mlp(train, test, features, target, out):
    import warnings

    from sklearn.exceptions import ConvergenceWarning
    from sklearn.neural_network import MLPRegressor
    tr = train.dropna(subset=[target])
    y = (tr[target].groupby(tr["date"]).rank(pct=True) - 0.5).to_numpy()
    Xtr = cross_sectional_rank(tr, list(features)).to_numpy()
    Xte = cross_sectional_rank(test, list(features)).to_numpy()
    p = {k: v for k, v in PARAMS["mlp"].items() if k != "seeds"}
    preds = []
    for seed in PARAMS["mlp"]["seeds"]:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ConvergenceWarning)
            preds.append(MLPRegressor(random_state=seed, **p).fit(Xtr, y).predict(Xte))
    out["score"] = np.mean(preds, axis=0)
    out["prob"] = np.nan
    return out
