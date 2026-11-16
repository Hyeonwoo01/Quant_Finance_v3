"""Model A(가격·거래량 v1)와 기준선의 Purged Walk-forward 실험 (가이드 7장). 비공개 저장소에서만 돈다.

    python -m evaluation.run_experiment --data-dir private_data --target RET_5D

- Vault: vault_start 이후 시세는 **읽지도 않는다** (패널을 그 전날까지만 불러온다). 그래서 vault 직전 판단 중
  청산 가격이 vault에 있는 표본도 자연히 빠진다.
- 유니버스 (판단 시점에 알 수 있는 정보만): 피처 행 기준 이력 252세션 이상, 그 행에 거래가 있었음,
  21세션 평균 거래대금이 그날 횡단면 하위 liquidity_pct 미만이면 제외.
- 출력: 모델·연도별 성능 요약만 (시세·종목별 값 없음). 실험 기록은 비공개 저장소 experiments/에 남긴다.
"""
import argparse
import json

import numpy as np
import sys
import time
from datetime import date, timedelta

import pandas as pd

from engines.prediction_engine import MODELS, PARAMS, fit_predict
from evaluation.experiment_registry import ExperimentRegistry, data_snapshot_id, list_hash
from evaluation import portfolio_eval
from evaluation.costs import SCENARIOS
from evaluation.costs import VERSION as COST_VERSION
from evaluation.metrics import evaluate, rank_ic
from evaluation.targets import TARGETS, build_targets
from evaluation.walkforward import assert_no_vault, make_folds, split
from feature_store.price_features import FEATURE_VERSION, FEATURES, decision_grid, features_at
from feature_store.price_panel import load_price_panel

ATTRIBUTION = "자료: 한국거래소 통계정보 (KRX Open API) — 모델 성능 요약이며 시세 값은 포함하지 않음"


def _round(v, n=4):
    return None if v is None or v != v else round(v, n)


def build_dataset(panel: pd.DataFrame, target_id: str, every: int, liquidity_pct: float, min_history: int):
    t = TARGETS[target_id]
    grid = decision_grid(panel, every=every)
    feats = features_at(panel, grid)
    tg = build_targets(panel, horizons=(t.horizon_sessions,))
    df = feats.merge(tg, on=["date", "code"], how="inner")
    if t.kind == "DIR":
        df["ret_raw"] = df["ret"]
    ok = (df["history"] >= min_history) & df["traded"].fillna(False).astype(bool) & df["log_adv_21"].notna()
    df = df[ok].copy()
    cut = df.groupby("date")["log_adv_21"].transform(lambda s: s.quantile(liquidity_pct))
    df = df[df["log_adv_21"] >= cut].copy()
    add_buckets(df)
    return df.reset_index(drop=True)


def add_buckets(df: pd.DataFrame) -> None:
    """판단 시점에 알 수 있는 정보로 나눈 구간 (가이드 7-8 국면별·구간별 분해, 5-5 국면은 PIT로).
    liq_q: 그날 21세션 평균 거래대금 5분위 (1 = 가장 적음). trend: 유니버스 평균 63세션 수익률의 부호.
    vol_regime: 유니버스 평균 21세션 변동성이 그 이전 판단일들의 중앙값보다 높은지 (과거 값만 씀)."""
    df["liq_q"] = df.groupby("date")["log_adv_21"].transform(lambda s: pd.qcut(s.rank(method="first"), 5, labels=False) + 1)
    mkt = df.groupby("date").agg(m63=("mom_63", "mean"), v21=("vol_21", "mean")).sort_index()
    past_med = mkt["v21"].expanding().median().shift(1)
    mkt["trend"] = np.where(mkt["m63"] > 0, "up", "down")
    mkt["vol_regime"] = np.where(past_med.isna(), "n/a", np.where(mkt["v21"] > past_med, "high", "low"))
    df["trend"] = df["date"].map(mkt["trend"])
    df["vol_regime"] = df["date"].map(mkt["vol_regime"])


def breakdown(p: pd.DataFrame, col: str) -> dict:
    """구간별 판단일 평균 Rank IC와 판단일 수."""
    out = {}
    for k, g in p.groupby(col):
        ic = rank_ic(g)
        out[str(k)] = {"ic_mean": _round(float(ic.mean())), "dates": int(ic.notna().sum())}
    return out


def run(data_dir, target_id: str = "RET_5D", *, start: date = date(2010, 1, 1), first_test: date = date(2016, 1, 1),
        vault_start: date = date(2024, 11, 1), every: int = 5, models=MODELS, liquidity_pct: float = 0.2,
        min_history: int = 252, embargo_sessions: int = 5, today=None, record: bool = True, log=print,
        top: float = 0.1, keep: float = None, notional: float = 1e8) -> dict:
    t0 = time.time()
    t = TARGETS[target_id]
    panel = load_price_panel(data_dir, start, vault_start - timedelta(days=1), today=today)
    df = build_dataset(panel, target_id, every, liquidity_pct, min_history)
    assert_no_vault(df, vault_start)
    folds = make_folds(df["date"], first_test=first_test, vault_start=vault_start, embargo_sessions=embargo_sessions)
    log(f"표본 {len(df):,}행, 판단일 {df['date'].nunique():,}일, 종목 {df['code'].nunique():,}개, 폴드 {len(folds)}개 "
        f"({time.time() - t0:.0f}초)")
    preds = {m: [] for m in models}
    for f in folds:
        tr, te = split(df, f)
        for m in models:
            p = fit_predict(m, tr, te, FEATURES, target="ret")
            preds[m].append(p.assign(fold=f.fold_id))
        log(f"  폴드 {f.fold_id}: 검증 {f.test_start.date()}~{f.test_end.date()}, 학습 {len(tr):,}행, 검증 {len(te):,}행")
    cols = ["date", "code", "ret", "tradable", "ret_delist", "entry_open", "entry_no_trade", "entry_limit_up",
            "exit_no_trade", "exit_limit_down", "no_exit_row", "market", "log_adv_21", "liq_q", "trend", "vol_regime"]
    results, by_year, by_bucket, portfolios = {}, {}, {}, {}
    for m in models:
        p = pd.concat(preds[m], ignore_index=True).merge(df[[c for c in cols if c in df]], on=["date", "code"])
        prob = "prob" if p["prob"].notna().any() else None
        results[m] = evaluate(p, horizon=t.horizon_sessions, step=every, prob=prob)
        by_year[m] = {int(y): _round(evaluate(g, horizon=t.horizon_sessions, step=every)["ic_mean"])
                      for y, g in p.groupby(p["date"].dt.year)}
        by_bucket[m] = {c: breakdown(p, c) for c in ("liq_q", "market", "trend", "vol_regime") if c in p}
        if m != "uncond" and every == t.horizon_sessions and t.kind == "RET":      # 겹치지 않는 보유일 때만
            portfolios[m] = {sc.name: portfolio_eval.summarize(
                portfolio_eval.long_only(p, h=t.horizon_sessions, top=top, keep=keep, notional=notional, scenario=sc),
                h=t.horizon_sessions) for sc in SCENARIOS}
    out = {"target_definition_id": target_id, "target": t.__dict__, "feature_schema_version": FEATURE_VERSION,
           "feature_list_hash": list_hash(FEATURES), "features": list(FEATURES), "models": list(models),
           "hyperparameters": {m: PARAMS.get(m, {}) for m in models}, "data_snapshot_id": data_snapshot_id(data_dir),
           "universe": {"liquidity_pct": liquidity_pct, "min_history": min_history, "every_sessions": every},
           "fold_definition": {"type": "expanding", "test_months": 12, "first_test": str(first_test),
                               "vault_start": str(vault_start), "embargo_sessions": embargo_sessions,
                               "purge": "label_available_at < first test decision time",
                               "folds": [[str(f.test_start.date()), str(f.test_end.date())] for f in folds]},
           "sample": {"rows": len(df), "dates": int(df["date"].nunique()), "codes": int(df["code"].nunique())},
           "metrics": results, "ic_by_year": by_year, "ic_by_bucket": by_bucket,
           "portfolio": {"type": "long_only_top", "top": top, "keep": keep, "notional_krw": notional, "results": portfolios},
           "cost_model_version": COST_VERSION,
           "seconds": round(time.time() - t0)}
    if record:
        rec = ExperimentRegistry(data_dir).record(out)
        out["experiment_id"] = rec["experiment_id"]
        out["prior_trials_same_target"] = ExperimentRegistry(data_dir).count_trials(target_id)
    return out


def report(out: dict) -> str:
    lines = [f"실험 {out.get('experiment_id', '-')}: {out['target_definition_id']} "
             f"(표본 {out['sample']['rows']:,}행·{out['sample']['dates']}일, 폴드 {len(out['fold_definition']['folds'])}개, "
             f"Vault {out['fold_definition']['vault_start']}~ 미조회)",
             f"{'모델':10s} {'IC평균':>8s} {'ICIR':>7s} {'t(NW)':>7s} {'IC>0':>6s} {'상하위10%':>9s} {'t(NW)':>7s} {'방향적중':>8s} {'Brier':>7s}"]
    for m, r in out["metrics"].items():
        def f(k, fmt):
            v = r.get(k)
            width = int(fmt.split(".")[0])
            return format(v, fmt) if isinstance(v, (int, float)) and v == v else "—".rjust(width)
        lines.append(f"{m:10s} {f('ic_mean', '8.4f')} {f('icir', '7.3f')} {f('ic_t_nw', '7.2f')} {f('ic_positive_share', '6.2f')} "
                     f"{f('top_bottom_decile', '9.4f')} {f('top_bottom_t_nw', '7.2f')} {f('dir_accuracy', '8.4f')} {f('brier', '7.4f')}")
    lines.append("연도별 IC 평균: " + json.dumps(out["ic_by_year"], ensure_ascii=False))
    for m, b in out.get("ic_by_bucket", {}).items():
        if m in ("ridge", "gbm", "reversal"):
            lines.append(f"구간별 IC [{m}]: " + json.dumps({c: {k: v["ic_mean"] for k, v in d.items()} for c, d in b.items()},
                                                        ensure_ascii=False))
    pf = out.get("portfolio", {}).get("results", {})
    if pf:
        pk = out["portfolio"].get("keep") or out["portfolio"]["top"]
        lines.append(f"Long-only 상위 {out['portfolio']['top']:.0%} 매수·상위 {pk:.0%} 밖이면 매도 "
                     f"(운용금액 {out['portfolio']['notional_krw']:,.0f}원, 연율):")
        lines.append(f"  {'모델':10s} {'시나리오':12s} {'비용전':>8s} {'순수익':>8s} {'기준':>8s} {'초과':>8s} {'샤프':>6s} {'초과샤프':>8s} "
                     f"{'MDD':>7s} {'회전율':>6s} {'비용/회':>7s} {'현금':>6s} {'종목수':>6s}")
        for m, scs in pf.items():
            for sc, r in scs.items():
                if not r:
                    continue
                lines.append(f"  {m:10s} {sc:12s} {r['ann_gross']:8.2%} {r['ann_net']:8.2%} {r['ann_bench']:8.2%} {r['ann_excess']:8.2%} "
                             f"{r['sharpe_net']:6.2f} {r['sharpe_excess']:8.2f} {r['mdd_net']:7.1%} {r['avg_turnover']:6.2f} "
                             f"{r['avg_cost']:7.3%} {r['avg_cash']:6.1%} {r['avg_names']:6.0f}")
        g = pf.get("gbm", {}).get("base")
        if g:
            lines.append("  gbm·base 연도별 초과수익: " + json.dumps(g["excess_by_year"], ensure_ascii=False))
    if "prior_trials_same_target" in out:
        lines.append(f"같은 타깃 누적 시행 수(모델 기준): {out['prior_trials_same_target']} — 다중검정 보정에 쓴다")
    lines.append(ATTRIBUTION)
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default="private_data")
    ap.add_argument("--target", default="RET_5D", choices=sorted(TARGETS))
    ap.add_argument("--every", type=int, default=5, help="판단 간격 (세션)")
    ap.add_argument("--first-test", default="2016-01-01")
    ap.add_argument("--vault-start", default="2024-11-01")
    ap.add_argument("--models", default=",".join(MODELS))
    ap.add_argument("--top", type=float, default=0.1, help="Long-only 포트폴리오: 점수 상위 비율")
    ap.add_argument("--notional", type=float, default=1e8, help="Long-only 포트폴리오: 운용 금액(원), 용량 제약 계산용")
    ap.add_argument("--keep", type=float, default=None, help="교체 완충: 보유 종목은 상위 이 비율 안이면 유지 (기본: top과 같음)")
    ap.add_argument("--liquidity-pct", type=float, default=0.2, help="유니버스: 그날 거래대금 하위 이 비율 제외")
    args = ap.parse_args(argv)
    out = run(args.data_dir, args.target, first_test=date.fromisoformat(args.first_test),
              vault_start=date.fromisoformat(args.vault_start), every=args.every,
              models=tuple(m.strip() for m in args.models.split(",") if m.strip()), top=args.top, keep=args.keep,
              notional=args.notional, liquidity_pct=args.liquidity_pct)
    print(report(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
