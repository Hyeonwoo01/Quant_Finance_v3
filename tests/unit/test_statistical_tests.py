"""블록 부트스트랩, Newey-West t, 유효 시행 수, Deflated Sharpe."""
import numpy as np
import pytest

from evaluation.statistical_tests import (block_bootstrap_ci, deflated_sharpe, effective_trials, expected_max_sharpe,
                                          nw_t)


def test_bootstrap_ci_covers_mean_and_widens_with_autocorrelation():
    rng = np.random.default_rng(0)
    iid = rng.normal(0.01, 0.05, 400)
    lo, hi = block_bootstrap_ci(iid, block=5)
    assert lo < iid.mean() < hi and hi - lo < 0.02
    ar = np.zeros(400)
    for i in range(1, 400):
        ar[i] = 0.9 * ar[i - 1] + rng.normal(0, 0.05)
    w_iid = np.subtract(*block_bootstrap_ci(ar, block=1)[::-1])
    w_blk = np.subtract(*block_bootstrap_ci(ar, block=20)[::-1])
    assert w_blk > 1.5 * w_iid                       # 자기상관을 무시하면 신뢰구간이 너무 좁다
    assert np.isnan(block_bootstrap_ci([1.0, 2.0], block=5)[0])


def test_nw_t_shrinks_with_autocorrelation():
    rng = np.random.default_rng(1)
    x = np.repeat(rng.normal(0.05, 0.1, 100), 5)    # 같은 값이 5번씩 겹침 (겹치는 보유 기간 흉내)
    assert nw_t(x, 0) > 0 and nw_t(x, 5) < 0.7 * nw_t(x, 0)


def test_effective_trials():
    rng = np.random.default_rng(2)
    a = rng.normal(size=200)
    assert effective_trials([a, a, a]) == pytest.approx(1.0)
    ind = [rng.normal(size=200) for _ in range(5)]
    assert 4.5 < effective_trials(ind) <= 5.0
    assert effective_trials([a]) == 1.0


def test_deflated_sharpe_penalizes_many_trials():
    rng = np.random.default_rng(3)
    r = rng.normal(0.01, 0.05, 120)                # 기간 샤프 약 0.2
    one = deflated_sharpe(r, n_trials=1, sr_var=0.0)
    many = deflated_sharpe(r, n_trials=50, sr_var=0.04)
    assert one["dsr"] > 0.95 and many["dsr"] < one["dsr"] and many["sr_benchmark"] > 0
    assert expected_max_sharpe(1, 0.04) == 0.0
    assert np.isnan(deflated_sharpe([0.1] * 5, n_trials=3, sr_var=0.01)["dsr"])
