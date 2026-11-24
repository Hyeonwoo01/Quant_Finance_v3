"""Vault 평가 — docs/VAULT_PREREGISTRATION.md(확정 커밋 df7f546)의 값만으로 한 번만 돈다.

    python -m evaluation.vault_eval --data-dir private_data

- 확정 문서의 SHA-256이 아래 값과 같고 상태 줄이 "확정"일 때만 돈다 (문서를 바꾸면 거부).
- 실험 기록에 Vault 평가가 이미 있으면 거부한다 (한 번만).
- 인자는 데이터 위치뿐이다. 모델·피처·비용·기준·검정은 모두 아래 상수이며 사전 등록 문서와 같다.
- 기존 실험 코드(run_experiment·prediction_engine·portfolio_eval·costs)를 그대로 쓴다. 다른 점은 판단일뿐이다:
  개발 구간은 실험과 같이 패널 첫 세션부터 21세션마다, Vault는 2024-11-01 이후 첫 세션부터 21세션마다.
- 출력은 성과 요약뿐이다 (시세 값 없음). 자료: 한국거래소 통계정보.
"""
import argparse
import hashlib
import math
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from core.calendar_align import kr_decision_cutoff
from engines.prediction_engine import combine, fit_predict
from evaluation import portfolio_eval
from evaluation.costs import SCENARIOS
from evaluation.costs import VERSION as COST_VERSION
from evaluation.experiment_registry import ExperimentRegistry, data_snapshot_id
from evaluation.metrics import evaluate, rank_ic
from evaluation.run_experiment import add_buckets
from evaluation.statistical_tests import nw_t
from evaluation.targets import build_targets
from evaluation.walkforward import make_folds, split
from feature_store.price_features import FEATURES, FEATURE_VERSION, features_at, market_state
from feature_store.price_panel import load_price_panel

PREREG_PATH = Path(__file__).resolve().parents[1] / "docs" / "VAULT_PREREGISTRATION.md"
PREREG_COMMIT = "df7f546fea15a6d4befacdd5c0c7fab9076a15e9"
PREREG_SHA256 = "4b55904ff2ed3cc5c9ba8181f01c12b95eeed5fe5ce9116f5a7ba9de4fc1d6c4"
KIND = "vault_evaluation"

# ---- 사전 등록 값 (문서 2~4절) ----
START = date(2010, 1, 1)
VAULT_START = date(2024, 11, 1)
H = 21                      # RET_21D, 판단 간격 21세션
LIQUIDITY_PCT = 0.2
MIN_HISTORY = 252
TOP, KEEP, NOTIONAL = 0.1, 0.2, 1e8
EMBARGO = 5
TEST_MONTHS = 12            # Vault 안에서 12개월마다 다시 학습
CANDIDATES = (("H1", "ens", ("ridge", "gbm")), ("H2", "mlp", ("mlp",)))
ALPHA = 0.05                # 순차 검정: H1 → (통과 시) H2, 각각 단측 0.05
BOOT_BLOCK, BOOT_N, BOOT_SEED = 6, 10_000, 0
IC_T_MIN = 1.65


def check_prereg(path: Path = PREREG_PATH, sha: str = PREREG_SHA256) -> None:
    text = path.read_bytes()
    got = hashlib.sha256(text).hexdigest()
    if got != sha:
        raise PermissionError(f"사전 등록 문서가 확정 뒤 바뀌었다 (SHA-256 {got[:12]} ≠ {sha[:12]}) — Vault 평가 거부")
    if "**상태: 확정" not in text.decode("utf-8"):
        raise PermissionError("사전 등록 문서가 확정 상태가 아니다 — Vault 평가 거부")


def already_done(registry: ExperimentRegistry) -> bool:
    return any(r.get("kind") == KIND for r in registry.all())


def decision_dates(panel: pd.DataFrame, vault_start: date) -> tuple:
    """(개발 판단일, Vault 판단일). Vault는 정답(D+21 시가)이 패널 안에 있는 판단일까지만."""
    sessions = pd.DatetimeIndex(sorted(panel["date"].unique()))
    dev = sessions[sessions < pd.Timestamp(vault_start)][::H]
    pos = np.flatnonzero(sessions >= pd.Timestamp(vault_start))
    vault_pos = pos[::H]
    vault_pos = vault_pos[vault_pos + H <= len(sessions) - 1]
    return dev, sessions[vault_pos]


def build(panel: pd.DataFrame, dates) -> pd.DataFrame:
    """run_experiment.build_dataset과 같은 처리(시점 기준 피처·타깃·유니버스 필터)를 주어진 판단일로."""
    cols = ["date", "code"] + (["market"] if "market" in panel else [])
    g = panel[panel["date"].isin(dates)][cols].copy()
    cut = {d: kr_decision_cutoff(pd.Timestamp(d)) for d in dates}
    g["decision_time_utc"] = g["date"].map(cut)
    feats = market_state(features_at(panel, g))
    tg = build_targets(panel, horizons=(H,))
    df = feats.merge(tg, on=["date", "code"], how="inner")
    ok = (df["history"] >= MIN_HISTORY) & df["traded"].fillna(False).astype(bool) & df["log_adv_21"].notna()
    df = df[ok].copy()
    q = df.groupby("date")["log_adv_21"].transform(lambda s: s.quantile(LIQUIDITY_PCT))
    df = df[df["log_adv_21"] >= q].copy()
    add_buckets(df)
    return df.reset_index(drop=True)


def bootstrap_p_one_sided(x) -> float:
    """H0: 평균 초과수익 ≤ 0. 평균을 뺀 계열을 블록 부트스트랩해 관측 평균 이상이 나올 비율."""
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 2 * BOOT_BLOCK:
        return float("nan")
    xc = x - x.mean()
    rng = np.random.default_rng(BOOT_SEED)
    k = math.ceil(n / BOOT_BLOCK)
    starts = rng.integers(0, n, size=(BOOT_N, k))
    idx = (starts[:, :, None] + np.arange(BOOT_BLOCK)[None, None, :]) % n
    means = xc[idx.reshape(BOOT_N, -1)[:, :n]].mean(axis=1)
    return float((np.sum(means >= x.mean()) + 1) / (BOOT_N + 1))


def judge(port: dict, ic: pd.Series, series: list) -> dict:
    ic = ic.dropna()
    p = bootstrap_p_one_sided(series)
    t_ic = nw_t(ic.to_numpy(), 1) if len(ic) > 2 else float("nan")
    c1 = bool(np.mean(series) > 0 and p < ALPHA)
    c2 = bool(port["conservative"]["ann_excess"] > 0)
    c3 = bool(ic.mean() > 0 and t_ic > IC_T_MIN)
    return {"p_one_sided": p, "mean_excess_per_period": float(np.mean(series)), "ic_mean": float(ic.mean()),
            "ic_t_nw": t_ic, "c1_excess": c1, "c2_conservative": c2, "c3_ic": c3, "pass": c1 and c2 and c3}


def run(data_dir, *, loader=load_price_panel, vault_start: date = VAULT_START, prereg_path: Path = PREREG_PATH,
        prereg_sha: str = PREREG_SHA256, log=print) -> dict:
    check_prereg(prereg_path, prereg_sha)
    reg = ExperimentRegistry(data_dir)
    if already_done(reg):
        raise PermissionError("Vault 평가가 이미 실험 기록에 있다 — 한 번만 한다 (사전 등록 5절)")
    panel = loader(data_dir, START, date.today())
    dev, vdates = decision_dates(panel, vault_start)
    if len(vdates) == 0:
        raise ValueError("정답이 있는 Vault 판단일이 없다")
    df = build(panel, dev.append(vdates))
    folds = make_folds(df["date"], first_test=vault_start, vault_start=(vdates[-1] + pd.Timedelta(days=1)).date(),
                       test_months=TEST_MONTHS, embargo_sessions=EMBARGO)
    log(f"Vault 판단일 {df.loc[df['date'] >= pd.Timestamp(vault_start), 'date'].nunique()}개 "
        f"({vdates[0].date()} ~ {vdates[-1].date()}), 폴드 {len(folds)}개, 표본 {len(df):,}행")
    needed = sorted({m for _, _, parts in CANDIDATES for m in parts})
    preds = {name: [] for _, name, _ in CANDIDATES}
    for f in folds:
        tr, te = split(df, f)
        cache = {m: fit_predict(m, tr, te, list(FEATURES), target="ret") for m in needed}
        for _, name, parts in CANDIDATES:
            p = combine([cache[m] for m in parts]) if len(parts) > 1 else cache[parts[0]]
            preds[name].append(p)
        log(f"  폴드 {f.fold_id}: 검증 {f.test_start.date()}~{f.test_end.date()}, 학습 {len(tr):,}행, 검증 {len(te):,}행")
    cols = ["date", "code", "ret", "tradable", "ret_delist", "entry_open", "entry_no_trade", "entry_limit_up",
            "exit_no_trade", "exit_limit_down", "no_exit_row", "market", "log_adv_21", "liq_q", "trend", "vol_regime"]
    results, sequential_ok = {}, True
    for label, name, _ in CANDIDATES:
        p = pd.concat(preds[name], ignore_index=True).merge(df[[c for c in cols if c in df]], on=["date", "code"])
        port, series = {}, None
        for sc in SCENARIOS:
            res = portfolio_eval.long_only(p, h=H, top=TOP, keep=KEEP, notional=NOTIONAL, scenario=sc)
            port[sc.name] = portfolio_eval.summarize(res, h=H)
            if sc.name == "base":
                series = [float(x) for x in res["excess"]]
                dates = [str(d.date()) for d in res["date"]]
        prob = "prob" if p["prob"].notna().any() else None
        metrics = evaluate(p, horizon=H, step=H, prob=prob)
        j = judge(port, rank_ic(p), series)
        judged = sequential_ok                     # 순차 검정: 앞 후보가 통과해야 판정
        j["judged"] = judged
        j["verdict"] = ("검증된 신호" if j["pass"] else "탐색 결과") if judged else "판정 안 함 (H1 실패, 보고만)"
        sequential_ok = sequential_ok and j["pass"]
        results[label] = {"model": name, "judgement": j, "portfolio": port, "metrics": metrics,
                          "series": {"dates": dates, "excess_base": series}}
    out = {"kind": KIND, "target_definition_id": "RET_21D", "prereg_commit": PREREG_COMMIT, "prereg_sha256": prereg_sha,
           "evaluated_at_utc": datetime.now(timezone.utc).isoformat(), "data_snapshot_id": data_snapshot_id(data_dir),
           "feature_schema_version": FEATURE_VERSION, "cost_model_version": COST_VERSION,
           "vault": {"start": str(vault_start), "decisions": [str(d.date()) for d in vdates],
                     "folds": [[str(f.test_start.date()), str(f.test_end.date())] for f in folds], "results": results}}
    rec = reg.record(out)
    out["experiment_id"] = rec["experiment_id"]
    return out


def report(out: dict) -> str:
    v = out["vault"]
    lines = [f"Vault 평가 {out.get('experiment_id', '-')} — 사전 등록 {out['prereg_commit'][:7]} "
             f"(판단일 {len(v['decisions'])}개: {v['decisions'][0]} ~ {v['decisions'][-1]})"]
    for label, r in v["results"].items():
        j, pt = r["judgement"], r["portfolio"]
        lines.append(f"{label} {r['model']}: {j['verdict']}")
        lines.append(f"  1) base 비용 후 초과수익 연율 {pt['base']['ann_excess']:+.1%}, 단측 p {j['p_one_sided']:.3f} "
                     f"(기준 < {ALPHA}) → {'통과' if j['c1_excess'] else '미달'}")
        lines.append(f"  2) conservative 초과 {pt['conservative']['ann_excess']:+.1%} → {'통과' if j['c2_conservative'] else '미달'}")
        lines.append(f"  3) Rank IC 평균 {j['ic_mean']:.4f}, NW t {j['ic_t_nw']:.2f} (기준 > {IC_T_MIN}) → "
                     f"{'통과' if j['c3_ic'] else '미달'}")
        lines.append(f"  보고만: stress 초과 {pt['stress']['ann_excess']:+.1%}, MDD {pt['base']['mdd_net']:.1%}, "
                     f"회전율 {pt['base']['avg_turnover']:.2f}, 연도별 초과 {pt['base']['excess_by_year']}")
    lines.append("자료: 한국거래소 통계정보 (KRX Open API) — 모델 성능 요약이며 시세 값은 포함하지 않음")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", required=True)
    args = ap.parse_args(argv)
    print(report(run(args.data_dir)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
