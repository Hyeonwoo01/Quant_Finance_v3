"""Vault 후보 선택 — 가이드 8-5 "Vault 후보 선택 규칙"(2026-11-24, 결과를 보기 전에 적음)을 그대로 적용한다.

    python -m evaluation.select_candidates --data-dir private_data

규칙: 같은 타깃·설정(RET_21D, 판단 간격 21, 교체 완충 0.2, 유니버스 하위 20% 제외)의 모든 시행 중
  1) base 비용 후 기간별 초과수익의 연율 95% 블록 부트스트랩 신뢰구간 하한 > 0
  2) conservative 비용 후 연율 초과수익 > 0
을 만족하는 것만 남기고, 기간 초과 샤프(평균 ÷ 표준편차)가 가장 높은 것을 H1, H1과 기간별 초과수익 상관이
0.7 미만인 것 중 가장 높은 것을 H2로 한다 (없으면 H1 하나).
실험 기록만 읽는다 (시세를 읽지 않음). 출력은 성과 요약뿐이다.
"""
import argparse

import numpy as np
import pandas as pd

from evaluation.experiment_registry import ExperimentRegistry
from evaluation.statistical_tests import block_bootstrap_ci

SETTING = {"target": "RET_21D", "every": 21, "keep": 0.2, "liquidity_pct": 0.2}
MAX_CORR = 0.7


def trials(records: list, setting: dict = SETTING) -> list:
    out = []
    for r in records:
        if (r.get("target_definition_id") != setting["target"]
                or (r.get("universe") or {}).get("every_sessions") != setting["every"]
                or (r.get("portfolio") or {}).get("keep") != setting["keep"]
                or (r.get("universe") or {}).get("liquidity_pct") != setting["liquidity_pct"]):
            continue
        h = (r.get("target") or {}).get("horizon_sessions", 21)
        per_year = 252 / h
        for m, s in (r.get("series") or {}).items():
            x = np.asarray(s.get("excess_base") or [], dtype=float)
            if len(x) < 3 or np.std(x, ddof=1) == 0:
                continue
            lo, hi = block_bootstrap_ci(x, block=max(2, int(round(per_year / 4))))
            cons = (((r.get("portfolio") or {}).get("results") or {}).get(m) or {}).get("conservative", {}).get("ann_excess")
            tag = r.get("feature_schema_version", "?") + (f" 학습 {r['train_start']}~" if r.get("train_start") else "")
            out.append({"id": f"{r.get('experiment_id', '?')}:{m}", "model": m, "features": tag,
                        "sr": float(x.mean() / np.std(x, ddof=1)), "ci_lo": lo * per_year, "ci_hi": hi * per_year,
                        "cons_excess": cons, "series": pd.Series(x, index=pd.to_datetime(s["dates"]))})
    return out


def select(ts: list, max_corr: float = MAX_CORR) -> dict:
    ok = [t for t in ts if t["ci_lo"] > 0 and t["cons_excess"] is not None and t["cons_excess"] > 0]
    ok.sort(key=lambda t: t["sr"], reverse=True)
    if not ok:
        return {"eligible": [], "h1": None, "h2": None}
    h1 = ok[0]
    for t in ok:
        both = pd.concat([h1["series"], t["series"]], axis=1, join="inner")
        t["corr_h1"] = float(both.iloc[:, 0].corr(both.iloc[:, 1])) if len(both) > 2 else float("nan")
    h2 = next((t for t in ok[1:] if t["corr_h1"] < max_corr), None)
    return {"eligible": ok, "h1": h1, "h2": h2}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", required=True)
    args = ap.parse_args(argv)
    ts = trials(ExperimentRegistry(args.data_dir).all())
    res = select(ts)
    print(f"같은 타깃·설정의 시행 {len(ts)}개 중 조건(신뢰구간 하한 > 0, conservative 초과 > 0)을 만족한 것 {len(res['eligible'])}개")
    print(f"{'시행':<38} {'피처':<26} {'기간 샤프':>8} {'95% 구간(연율)':>18} {'보수 초과':>8} {'H1 상관':>7}")
    for t in res["eligible"]:
        print(f"{t['id']:<38} {t['features']:<26} {t['sr']:>8.3f} [{t['ci_lo']:+.1%}, {t['ci_hi']:+.1%}] "
              f"{t['cons_excess']:>+8.1%} {t.get('corr_h1', float('nan')):>7.2f}")
    for k in ("h1", "h2"):
        t = res[k]
        print(f"{k.upper()}: " + (f"{t['id']} ({t['model']}, {t['features']}, 기간 샤프 {t['sr']:.3f})" if t else "없음"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
