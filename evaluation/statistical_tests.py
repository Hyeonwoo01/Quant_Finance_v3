"""모델·전략 비교 검정 (가이드 7-8 "적절한 모델 비교 검정", 7-12 유효 시행 수).

- block_bootstrap_ci: 시계열 의존성을 고려한 원형 블록 부트스트랩 신뢰구간 (평균).
- nw_t: 평균이 0인지 Newey-West t (겹치는 보유 기간·자기상관 반영). 판단일별 IC 차이에 쓰면
  손실 차이 대신 IC 차이를 쓰는 Diebold-Mariano 형태의 비교가 된다.
- effective_trials: 시행들의 수익률 시계열 상관에서 유효 시행 수 추정 (서로 비슷한 변형은 독립 시행이 아니다).
- deflated_sharpe: Bailey & López de Prado (2014) Deflated Sharpe Ratio. 시행 수와 시행 간 샤프 분산으로
  "가장 좋은 것을 골랐을 때 운으로 기대되는 최대 샤프"를 기준선으로 삼아, 관측 샤프가 그보다 클 확률을 낸다.
  샤프는 기간 단위(연율화하지 않음)로 넣는다.
"""
import math

import numpy as np
from scipy import stats

EULER_GAMMA = 0.5772156649015329


def block_bootstrap_ci(x, *, block: int = 6, n_boot: int = 5000, alpha: float = 0.05, seed: int = 0) -> tuple:
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 2 * block:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    k = math.ceil(n / block)
    starts = rng.integers(0, n, size=(n_boot, k))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]) % n
    means = x[idx.reshape(n_boot, -1)[:, :n]].mean(axis=1)
    return float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2))


def nw_t(x, lags: int) -> float:
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 3:
        return float("nan")
    e = x - x.mean()
    s = (e @ e) / n
    for k in range(1, min(lags, n - 1) + 1):
        s += 2 * (1 - k / (lags + 1)) * (e[k:] @ e[:-k]) / n
    return float(x.mean() / math.sqrt(s / n)) if s > 0 else float("nan")


def effective_trials(series: list) -> float:
    """시행별 수익률 시계열(같은 날짜 정렬, 길이 같음)의 평균 상관 ρ로 N_eff = ρ + (1 − ρ)·N.
    (완전히 같으면 1, 서로 독립이면 N)"""
    m = [np.asarray(s, dtype=float) for s in series if s is not None and len(s) > 2]
    n = len(m)
    if n <= 1:
        return float(n)
    length = min(len(s) for s in m)
    mat = np.vstack([s[-length:] for s in m])
    c = np.corrcoef(mat)
    rho = float(np.nanmean(c[np.triu_indices(n, 1)]))
    rho = min(max(rho, 0.0), 1.0)
    return rho + (1 - rho) * n


def expected_max_sharpe(n_trials: float, sr_var: float) -> float:
    if n_trials <= 1 or sr_var <= 0:
        return 0.0
    z1 = stats.norm.ppf(1 - 1 / n_trials)
    z2 = stats.norm.ppf(1 - 1 / (n_trials * math.e))
    return math.sqrt(sr_var) * ((1 - EULER_GAMMA) * z1 + EULER_GAMMA * z2)


def deflated_sharpe(returns, *, n_trials: float, sr_var: float) -> dict:
    """관측 기간 수익률(초과수익 등)의 DSR. sr_var: 시행들 사이 (기간 단위) 샤프의 분산."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    n = len(r)
    if n < 10 or r.std(ddof=1) == 0:
        return {"sr": float("nan"), "sr_benchmark": float("nan"), "dsr": float("nan"), "n_obs": n}
    sr = r.mean() / r.std(ddof=1)
    skew, kurt = float(stats.skew(r)), float(stats.kurtosis(r, fisher=False))
    sr0 = expected_max_sharpe(n_trials, sr_var)
    denom = math.sqrt(max(1e-12, 1 - skew * sr + (kurt - 1) / 4 * sr ** 2))
    z = (sr - sr0) * math.sqrt(n - 1) / denom
    return {"sr": float(sr), "sr_benchmark": float(sr0), "dsr": float(stats.norm.cdf(z)), "n_obs": n,
            "skew": skew, "kurtosis": kurt, "n_trials": float(n_trials)}
