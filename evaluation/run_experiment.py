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
import math

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
from evaluation.statistical_tests import block_bootstrap_ci, deflated_sharpe, effective_trials, nw_t
from evaluation.targets import TARGETS, build_targets
from evaluation.walkforward import assert_no_vault, make_folds, split
from feature_store import fundamental_features as fund
from feature_store.price_features import (FEATURE_VERSION, FEATURE_VERSION_V2, FEATURES, FEATURES_V2, MARKET_FEATURES,
                                         MARKET_VERSION, decision_grid,
                                          features_at, market_state)
from feature_store.price_panel import load_price_panel

ATTRIBUTION = "자료: 한국거래소 통계정보 (KRX Open API) — 모델 성능 요약이며 시세 값은 포함하지 않음"


def _round(v, n=4):
    return None if v is None or v != v else round(v, n)


def build_dataset(panel: pd.DataFrame, target_id: str, every: int, liquidity_pct: float, min_history: int,
                  reports: pd.DataFrame = None):
    t = TARGETS[target_id]
    grid = decision_grid(panel, every=every)
    feats = market_state(features_at(panel, grid))
    if reports is not None:
        feats = fund.fundamentals_at(feats, reports)
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
        top: float = 0.1, keep: float = None, notional: float = 1e8, public_dir=None, market: bool = False,
        feature_set: str = "v1", train_start: date = None) -> dict:
    """feature_set: v1 = price_v1, v2 = price_v2 (이상현상 피처 4개 추가).
    train_start: 주면 모든 모델의 학습을 이 날짜 이후 판단일로만 (예: 재무가 있는 구간만 학습). 검증 폴드는 그대로."""
    t0 = time.time()
    t = TARGETS[target_id]
    panel = load_price_panel(data_dir, start, vault_start - timedelta(days=1), today=today)
    reports = fund.report_table(fund.load_fin_hist(public_dir)) if public_dir else None
    price_feats, price_ver = {"v1": (FEATURES, FEATURE_VERSION), "v2": (FEATURES_V2, FEATURE_VERSION_V2)}[feature_set]
    features = list(price_feats) + (list(fund.FUND_FEATURES) if reports is not None else [])
    version = price_ver + (f"+{fund.FUND_VERSION}" if reports is not None else "") + (f"+{MARKET_VERSION}" if market else "")
    raw = list(MARKET_FEATURES) if market else []
    df = build_dataset(panel, target_id, every, liquidity_pct, min_history, reports=reports)
    if reports is not None:
        log(f"재무 피처 커버리지(연도별 비율): {fund.coverage(df)}, 단위 오류 의심으로 bm·ep·sp를 비운 비율 {fund.unit_suspect_rate(df)}")
    assert_no_vault(df, vault_start)
    folds = make_folds(df["date"], first_test=first_test, vault_start=vault_start, embargo_sessions=embargo_sessions)
    log(f"표본 {len(df):,}행, 판단일 {df['date'].nunique():,}일, 종목 {df['code'].nunique():,}개, 폴드 {len(folds)}개 "
        f"({time.time() - t0:.0f}초)")
    preds = {m: [] for m in models}
    for f in folds:
        tr, te = split(df, f)
        if train_start is not None:
            tr = tr[tr["date"] >= pd.Timestamp(train_start)]
        for m in models:
            p = fit_predict(m, tr, te, features, target="ret", raw_features=raw)
            preds[m].append(p.assign(fold=f.fold_id))
        log(f"  폴드 {f.fold_id}: 검증 {f.test_start.date()}~{f.test_end.date()}, 학습 {len(tr):,}행, 검증 {len(te):,}행")
    cols = ["date", "code", "ret", "tradable", "ret_delist", "entry_open", "entry_no_trade", "entry_limit_up",
            "exit_no_trade", "exit_limit_down", "no_exit_row", "market", "log_adv_21", "liq_q", "trend", "vol_regime"]
    results, by_year, by_bucket, portfolios, series, ic_series = {}, {}, {}, {}, {}, {}
    for m in models:
        p = pd.concat(preds[m], ignore_index=True).merge(df[[c for c in cols if c in df]], on=["date", "code"])
        prob = "prob" if p["prob"].notna().any() else None
        results[m] = evaluate(p, horizon=t.horizon_sessions, step=every, prob=prob)
        by_year[m] = {int(y): _round(evaluate(g, horizon=t.horizon_sessions, step=every)["ic_mean"])
                      for y, g in p.groupby(p["date"].dt.year)}
        by_bucket[m] = {c: breakdown(p, c) for c in ("liq_q", "market", "trend", "vol_regime") if c in p}
        ic_series[m] = rank_ic(p)
        if m != "uncond" and every == t.horizon_sessions and t.kind == "RET":      # 겹치지 않는 보유일 때만
            portfolios[m] = {}
            for sc in SCENARIOS:
                res = portfolio_eval.long_only(p, h=t.horizon_sessions, top=top, keep=keep, notional=notional, scenario=sc)
                portfolios[m][sc.name] = portfolio_eval.summarize(res, h=t.horizon_sessions)
                if sc.name == "base":
                    series[m] = {"dates": [str(d.date()) for d in res["date"]],
                                 "excess_base": [round(float(x), 6) for x in res["excess"]]}
    tests = significance(data_dir, target_id, t.horizon_sessions, every, keep, liquidity_pct, series, ic_series)
    out = {"target_definition_id": target_id, "target": t.__dict__, "feature_schema_version": version,
           "feature_list_hash": list_hash(features + raw), "features": features, "raw_features": raw, "models": list(models),
           "fundamental_coverage": fund.coverage(df) if reports is not None else None,
           "fundamental_unit_suspect": fund.unit_suspect_rate(df) if reports is not None else None,
           "hyperparameters": {m: PARAMS.get(m.removesuffix("_rank"), {}) for m in models}, "data_snapshot_id": data_snapshot_id(data_dir),
           "train_start": str(train_start) if train_start else None,
           "universe": {"liquidity_pct": liquidity_pct, "min_history": min_history, "every_sessions": every},
           "fold_definition": {"type": "expanding", "test_months": 12, "first_test": str(first_test),
                               "vault_start": str(vault_start), "embargo_sessions": embargo_sessions,
                               "purge": "label_available_at < first test decision time",
                               "folds": [[str(f.test_start.date()), str(f.test_end.date())] for f in folds]},
           "sample": {"rows": len(df), "dates": int(df["date"].nunique()), "codes": int(df["code"].nunique())},
           "metrics": results, "ic_by_year": by_year, "ic_by_bucket": by_bucket,
           "portfolio": {"type": "long_only_top", "top": top, "keep": keep, "notional_krw": notional, "results": portfolios},
           "cost_model_version": COST_VERSION, "tests": tests, "series": series,
           "seconds": round(time.time() - t0)}
    if record:
        rec = ExperimentRegistry(data_dir).record(out)
        out["experiment_id"] = rec["experiment_id"]
        out["prior_trials_same_target"] = ExperimentRegistry(data_dir).count_trials(target_id)
    return out


def significance(data_dir, target_id, h, every, keep, liquidity_pct, series, ic_series) -> dict:
    """비용 후 초과수익의 신뢰구간·NW t, 모든 시행을 반영한 Deflated Sharpe, 모델 간 IC 차이 검정 (가이드 7-8, 7-12).
    시행 집합: 같은 타깃의 이전 실험 기록 + 이번 모델들. 이전 기록에 시계열이 있으면 상관으로 유효 시행 수를 낸다."""
    per_year = 252 / h
    lags = max(1, math.ceil(h / max(every, 1)))
    prior_sr, prior_series = [], []
    try:
        prior = [r for r in ExperimentRegistry(data_dir).all() if r.get("target_definition_id") == target_id]
    except Exception:                      # 기록을 못 읽어도 검정은 이번 시행만으로 낸다 (보수성은 줄어듦을 표시)
        prior = []
    for r in prior:
        hp = r.get("target", {}).get("horizon_sessions", h)
        for m, scs in (r.get("portfolio", {}).get("results") or {}).items():
            b = (scs or {}).get("base") or {}
            if b.get("sharpe_excess") == b.get("sharpe_excess") and b.get("sharpe_excess") is not None:
                prior_sr.append(b["sharpe_excess"] / math.sqrt(252 / hp))
        for m, sr in (r.get("series") or {}).items():
            prior_series.append(sr.get("excess_base"))
    cur_sr = {m: (np.mean(s["excess_base"]) / np.std(s["excess_base"], ddof=1)) for m, s in series.items()
              if len(s["excess_base"]) > 2 and np.std(s["excess_base"], ddof=1) > 0}
    all_sr = prior_sr + list(cur_sr.values())
    n_trials = len(all_sr)
    with_series = [x for x in prior_series if x] + [s["excess_base"] for s in series.values()]
    n_eff = effective_trials(with_series) if len(with_series) >= 2 else None
    sr_var = float(np.var(all_sr, ddof=1)) if len(all_sr) >= 2 else 0.0
    out = {"n_trials": n_trials, "n_trials_effective_from_series": n_eff, "sr_var_per_period": sr_var, "models": {}}
    for m, s in series.items():
        x = np.asarray(s["excess_base"], dtype=float)
        lo, hi = block_bootstrap_ci(x, block=max(2, int(round(per_year / 4))))
        dsr = deflated_sharpe(x, n_trials=max(n_trials, 1), sr_var=sr_var)
        out["models"][m] = {"mean_excess_per_period": float(x.mean()), "ci95_annualized": [lo * per_year, hi * per_year],
                            "t_nw": nw_t(x, lags), "dsr": dsr["dsr"], "sr_per_period": dsr["sr"],
                            "sr_benchmark": dsr["sr_benchmark"]}
    ic_diff = {}
    for a, b in (("gbm", "ridge"), ("gbm", "reversal"), ("ridge", "reversal"), ("gbm", "momentum")):
        if a in ic_series and b in ic_series:
            d = (ic_series[a] - ic_series[b]).dropna()
            ic_diff[f"{a}-{b}"] = {"mean": float(d.mean()), "t_nw": nw_t(d.to_numpy(), lags), "dates": len(d)}
    out["ic_difference"] = ic_diff
    return out


def report(out: dict) -> str:
    lines = [f"실험 {out.get('experiment_id', '-')}: {out['target_definition_id']} [{out['feature_schema_version']}] "
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
    ts = out.get("tests") or {}
    if ts.get("models"):
        lines.append(f"검정 (base 비용 후 초과수익, 같은 타깃 시행 {ts['n_trials']}개"
                     + (f", 시계열 상관으로 본 유효 시행 {ts['n_trials_effective_from_series']:.1f}개" if ts.get("n_trials_effective_from_series") else "")
                     + "):")
        for m, r in ts["models"].items():
            lo, hi = r["ci95_annualized"]
            lines.append(f"  {m:10s} 연율 95% 신뢰구간 [{lo:.2%}, {hi:.2%}], NW t {r['t_nw']:.2f}, "
                         f"Deflated Sharpe {r['dsr']:.3f} (기간 샤프 {r['sr_per_period']:.3f} vs 운으로 기대되는 최대 {r['sr_benchmark']:.3f})")
    if ts.get("ic_difference"):
        lines.append("IC 차이 검정 (판단일별, NW t): " + json.dumps({k: [round(v["mean"], 4), round(v["t_nw"], 2)]
                                                                for k, v in ts["ic_difference"].items()}, ensure_ascii=False))
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
    ap.add_argument("--public-dir", default=None, help="공개 data 브랜치 경로 — 주면 PIT 재무 피처(fund_v1)를 더한다")
    ap.add_argument("--market", action="store_true", help="시장 상태 피처(mkt_v1)를 gbm에 원값으로 더한다")
    ap.add_argument("--features", default="v1", choices=["v1", "v2"], help="시세 피처: v1 = price_v1, v2 = price_v2")
    ap.add_argument("--train-start", default=None, help="학습을 이 날짜 이후 판단일로만 (검증 폴드는 그대로)")
    args = ap.parse_args(argv)
    out = run(args.data_dir, args.target, first_test=date.fromisoformat(args.first_test),
              vault_start=date.fromisoformat(args.vault_start), every=args.every,
              models=tuple(m.strip() for m in args.models.split(",") if m.strip()), top=args.top, keep=args.keep,
              notional=args.notional, liquidity_pct=args.liquidity_pct, public_dir=args.public_dir, market=args.market,
              feature_set=args.features, train_start=date.fromisoformat(args.train_start) if args.train_start else None)
    print(report(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
