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
PARAMS = {
    "ridge": {"alpha": 10.0, "logit_C": 0.1},
    "gbm": {"max_iter": 200, "learning_rate": 0.05, "max_leaf_nodes": 31, "min_samples_leaf": 500,
            "l2_regularization": 1.0, "random_state": 0},
}


def cross_sectional_rank(df: pd.DataFrame, cols, by: str = "date") -> pd.DataFrame:
    out = df[cols].groupby(df[by]).rank(pct=True) - 0.5
    return out.fillna(0.0)


def fit_predict(model: str, train: pd.DataFrame, test: pd.DataFrame, features, target: str = "ret") -> pd.DataFrame:
    """test에 score(클수록 수익률이 높을 것)와 prob(상승 확률, 없으면 NaN)를 붙여 돌려준다."""
    out = test[["date", "code"]].copy()
    tr = train.dropna(subset=[target])
    y = tr[target].to_numpy()
    up = (y > 0).astype(int)
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
        reg = HistGradientBoostingRegressor(loss="absolute_error", **p).fit(Xtr, y)   # 꼬리가 두꺼운 수익률에 강건
        clf = HistGradientBoostingClassifier(**p).fit(Xtr, up)
    else:
        raise ValueError(f"알 수 없는 모델: {model}")
    out["score"] = reg.predict(Xte)
    out["prob"] = clf.predict_proba(Xte)[:, 1]
    return out
