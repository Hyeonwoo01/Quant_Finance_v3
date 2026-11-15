"""예측 평가 지표 (가이드 7-8). 판단일마다 횡단면으로 잰 뒤 시간 평균을 낸다.

- Rank IC: 판단일마다 예측 점수와 실현 수익률의 스피어만 상관. 평균, 표준편차, ICIR(평균/표준편차),
  t값은 겹치는 보유 기간을 고려해 Newey-West(시차 = 보유 세션 수를 판단 간격으로 나눈 값 이상)로 낸다.
- 방향 정확도: 상승 확률 0.5 기준 적중률과, 같은 표본의 무조건부 상승률(항상 다수 쪽을 찍는 기준) 대비 차이.
- Brier score: 상승 확률 예측의 제곱 오차. 학습 구간 상승률을 그대로 내는 기준선(uncond)의 값과 비교한다.
- 상·하위 10% 차이: 판단일마다 점수 상위 10%와 하위 10%의 평균 실현 수익률 차이 (비용·체결 전, 체결 가능 표본만).
이 값들은 시세가 아니라 모델 성능의 요약이다. 그래도 공개 로그에는 한국거래소 통계정보를 썼다는 표시를 함께 남긴다.
"""
import math

import numpy as np
import pandas as pd


def _nw_t(x: np.ndarray, lags: int) -> float:
    x = x[~np.isnan(x)]
    n = len(x)
    if n < 3:
        return float("nan")
    e = x - x.mean()
    s = (e @ e) / n
    for k in range(1, min(lags, n - 1) + 1):
        w = 1 - k / (lags + 1)
        s += 2 * w * (e[k:] @ e[:-k]) / n
    return float(x.mean() / math.sqrt(s / n)) if s > 0 else float("nan")


def rank_ic(df: pd.DataFrame, score: str = "score", target: str = "ret", by: str = "date") -> pd.Series:
    def one(g):
        g = g[[score, target]].dropna()
        if len(g) < 20 or g[score].nunique() < 2:
            return np.nan
        return g[score].rank().corr(g[target].rank())
    return df.groupby(by)[[score, target]].apply(one)


def evaluate(df: pd.DataFrame, *, horizon: int, step: int, score: str = "score", prob: str = None,
             target: str = "ret") -> dict:
    ic = rank_ic(df, score, target)
    lags = max(1, math.ceil(horizon / max(step, 1)))
    out = {"dates": int(ic.notna().sum()), "rows": int(df[target].notna().sum()),
           "ic_mean": float(ic.mean()), "ic_std": float(ic.std()),
           "icir": float(ic.mean() / ic.std()) if ic.std() > 0 else float("nan"),
           "ic_t_nw": _nw_t(ic.to_numpy(dtype=float), lags), "ic_positive_share": float((ic > 0).mean())}
    d = df.dropna(subset=[target])
    if "tradable" in d:
        t = d[d["tradable"]]
        def spread(g):
            if len(g) < 20:
                return np.nan
            q = g[score].rank(pct=True)
            return g.loc[q > 0.9, target].mean() - g.loc[q <= 0.1, target].mean()
        sp = t.groupby("date")[[score, target]].apply(spread)
        out["top_bottom_decile"] = float(sp.mean())
        out["top_bottom_t_nw"] = _nw_t(sp.to_numpy(dtype=float), lags)
    if prob is not None and prob in d:
        y = (d[target] > 0).astype(float)
        base = y.mean()
        out["up_rate"] = float(base)
        out["dir_accuracy"] = float(((d[prob] >= 0.5) == (y == 1)).mean())
        out["dir_accuracy_vs_majority"] = out["dir_accuracy"] - max(base, 1 - base)
        out["brier"] = float(((d[prob] - y) ** 2).mean())     # 기준: 학습 구간 상승률을 내는 uncond 모델의 brier
    return out
