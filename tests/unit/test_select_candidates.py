"""Vault 후보 선택 규칙: 조건 필터, 기간 샤프 1위 = H1, H1과 상관 0.7 미만 중 1위 = H2."""
import numpy as np

from evaluation.select_candidates import select, trials


def rec(eid, models, keep=0.2, every=21):
    rng = np.random.default_rng(abs(hash(eid)) % 1000)
    dates = [f"2020-{m:02d}-01" for m in range(1, 13)] + [f"2021-{m:02d}-01" for m in range(1, 13)]
    series, results = {}, {}
    for m, (mu, cons, base_noise) in models.items():
        x = mu + base_noise
        series[m] = {"dates": dates, "excess_base": list(x)}
        results[m] = {"conservative": {"ann_excess": cons}}
    return {"experiment_id": eid, "target_definition_id": "RET_21D", "target": {"horizon_sessions": 21},
            "universe": {"every_sessions": every, "liquidity_pct": 0.2}, "feature_schema_version": "price_v1",
            "portfolio": {"keep": keep, "results": results}, "series": series}


def test_rule_picks_best_then_least_correlated():
    rng = np.random.default_rng(0)
    common, other = rng.normal(0, 0.03, 24), rng.normal(0, 0.03, 24)
    recs = [rec("e1", {"a": (0.02, 0.05, common), "b": (0.018, 0.04, common + rng.normal(0, 0.003, 24)),
                       "c": (0.006, 0.03, other), "d": (0.03, -0.01, other)}),
            rec("e2", {"z": (0.05, 0.2, other)}, keep=None)]                # 다른 설정은 빼야 함
    res = select(trials(recs))
    ids = [t["id"] for t in res["eligible"]]
    assert "e1:d" not in ids and not any(i.startswith("e2") for i in ids)  # conservative ≤ 0, 다른 설정
    assert res["h1"]["id"] == "e1:a" and res["h2"]["id"] == "e1:c"         # b는 a와 상관이 높아 건너뜀
