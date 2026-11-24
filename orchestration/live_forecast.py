"""실전 예측 기록 — 매달 첫 거래일(D)의 판단을 D 09:00 전에 미리 남기고, 정답이 나오면 정산한다 (가이드 7-10).

    python -m orchestration.live_forecast --data-dir private_data --check     # 지금 예측할 차례인가 (데이터 없이)
    python -m orchestration.live_forecast --data-dir private_data --forecast  # 예측 기록 (차례가 아니면 건너뜀)
    python -m orchestration.live_forecast --data-dir private_data --settle    # 정답이 나온 예측 정산 + 실전 성적 요약

Vault 이후의 새 검증 데이터다. Vault 사전 등록(docs/VAULT_PREREGISTRATION.md)의 후보 두 개를 같은 설정으로 기록한다:
H1 = ens(ridge+gbm), H2 = mlp, 시세 피처 price_v1, 타깃 RET_21D (D 시가 진입, D+21세션 시가 청산).

- 판단 시각은 D 08:30 KST이고 그때 쓸 수 있는 시세는 D−2 종가까지다 (백테스트와 같은 가용 시각 규칙).
  그래서 D−1 오후부터 계산할 수 있다. GitHub 예약 실행이 몇 시간씩 늦어서 D−1 저녁~D 새벽에 여러 번 시도하고,
  처음 성공한 한 번만 기록한다 (이미 기록이 있으면 건너뜀).
- **소급 기록 금지:** D 09:00(장 시작) 뒤에는 장부가 기록을 거부한다. 그 달은 빠진 달로 남는다.
- **오래된 데이터로 몰래 예측하지 않는다:** 유니버스의 90% 이상이 D−2 종가를 갖고 있지 않으면 (아직 수집 전)
  기록하지 않고 다음 시도로 넘긴다.
- 학습: D 판단 시각 전에 정답이 나온 행만 (purge) + 판단일 5개 embargo. 판단일 격자는 실험과 같다
  (패널 첫 세션부터 21세션마다). 유니버스·필터도 실험과 같다.
- 출력은 건수·성적 요약뿐이다 (시세 값 없음). 기록은 비공개 저장소에만 쓴다.
"""
import argparse
import hashlib
import sys
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from core.calendar_align import KR_VENUE, calendar, kr_decision_cutoff
from engines.prediction_engine import combine, fit_predict
from evaluation.experiment_registry import data_snapshot_id, list_hash
from evaluation.forecast_audit import ForecastLedger, ForecastRecord, session_start_after
from evaluation.metrics import rank_ic
from evaluation.targets import build_targets
from feature_store.price_features import FEATURES, features_at, market_state
from feature_store.price_panel import load_price_panel

KST = ZoneInfo("Asia/Seoul")
START = date(2010, 1, 1)
H = 21
LIQUIDITY_PCT, MIN_HISTORY, EMBARGO = 0.2, 252, 5
MODELS = (("H1_ens_price_v1", ("ridge", "gbm")), ("H2_mlp_price_v1", ("mlp",)))
FRESH_SHARE = 0.9            # 유니버스 중 D−2 종가를 가진 비율이 이보다 낮으면 기록하지 않음
TOP = 0.1


def next_decision(now_utc: datetime) -> tuple:
    """(D, D가 그달 첫 거래일인가). D = 지금 이후 처음 시작하는(09:00 KST) KRX 세션."""
    start = session_start_after(now_utc)                          # 다음 세션 09:00 KST (UTC)
    d = pd.Timestamp(start).tz_convert(KST).date()
    cal = calendar(KR_VENUE)
    prev = cal.previous_session(pd.Timestamp(d)).date()
    return d, prev.month != d.month


def two_sessions_before(d: date) -> date:
    cal = calendar(KR_VENUE)
    return cal.previous_session(cal.previous_session(pd.Timestamp(d))).date()


def training_dates(panel: pd.DataFrame, d: date):
    sessions = pd.DatetimeIndex(sorted(panel["date"].unique()))
    return sessions[sessions < pd.Timestamp(d)][::H]


def _filters(df: pd.DataFrame) -> pd.DataFrame:
    ok = (df["history"] >= MIN_HISTORY) & df["traded"].fillna(False).astype(bool) & df["log_adv_21"].notna()
    df = df[ok].copy()
    q = df.groupby("date")["log_adv_21"].transform(lambda s: s.quantile(LIQUIDITY_PCT))
    return df[df["log_adv_21"] >= q].copy()


def build_live(panel: pd.DataFrame, d: date) -> tuple:
    """(학습 표본, D의 예측 표본, 신선도 비율). D의 행은 아직 없으므로 마지막 세션에 행이 있는 종목을 유니버스로 한다."""
    cut = kr_decision_cutoff(pd.Timestamp(d))
    # 학습: 실험과 같은 격자, D 판단 시각 전에 정답이 나온 행만
    tdates = training_dates(panel, d)
    cols = ["date", "code"] + (["market"] if "market" in panel else [])
    g = panel[panel["date"].isin(tdates)][cols].copy()
    g["decision_time_utc"] = g["date"].map({x: kr_decision_cutoff(pd.Timestamp(x)) for x in tdates})
    tr = market_state(features_at(panel, g)).merge(build_targets(panel, horizons=(H,)), on=["date", "code"], how="inner")
    tr = _filters(tr)
    lab = pd.to_datetime(tr["label_available_at"], utc=True)
    tr = tr[lab.notna() & (lab < cut)]
    keep_dates = sorted(tr["date"].unique())
    if len(keep_dates) > EMBARGO:
        tr = tr[tr["date"] <= keep_dates[-1 - EMBARGO]]
    # 예측: D 판단 시각 기준 피처
    last = panel["date"].max()
    u = panel[panel["date"] == last][cols].copy()
    u["date"] = pd.Timestamp(d)
    u["decision_time_utc"] = cut
    te = market_state(features_at(panel, u))
    want = pd.Timestamp(two_sessions_before(d))
    fresh = float((te["feature_date"] == want).mean()) if len(te) else 0.0
    te = _filters(te)
    return tr.reset_index(drop=True), te.reset_index(drop=True), fresh


def forecast(data_dir, now_utc: datetime = None, *, loader=load_price_panel, log=print,
             clock=lambda: datetime.now(timezone.utc)) -> dict:
    now_utc = now_utc or clock()
    d, first = next_decision(now_utc)
    if not first:
        log(f"다음 세션 {d}은 그달 첫 거래일이 아님 — 건너뜀")
        return {"status": "not_due", "decision": str(d)}
    open_utc = session_start_after(now_utc)                      # D 09:00 KST
    if clock() >= open_utc:
        log(f"{d} 장이 이미 열림 — 소급 기록 금지라 기록하지 않음")
        return {"status": "late", "decision": str(d)}
    ledger = ForecastLedger(data_dir)
    if any(f.forecast_id.startswith(f"{d}:") for f in ledger.forecasts()):
        log(f"{d} 판단은 이미 기록됨 — 건너뜀")
        return {"status": "already", "decision": str(d)}
    panel = loader(data_dir, START, now_utc.astimezone(KST).date())
    tr, te, fresh = build_live(panel, d)
    if fresh < FRESH_SHARE:
        log(f"{d} 판단에 필요한 {two_sessions_before(d)} 종가가 유니버스의 {fresh:.0%}에만 있음 (기준 {FRESH_SHARE:.0%}) "
            f"— 수집 전으로 보고 기록하지 않음 (다음 시도에서 다시)")
        return {"status": "stale", "decision": str(d), "fresh": fresh}
    cut = kr_decision_cutoff(pd.Timestamp(d)).to_pydatetime()
    data_cutoff = pd.to_datetime(te["bt_available_at"], utc=True).max().to_pydatetime()
    needed = sorted({m for _, parts in MODELS for m in parts})
    cache = {m: fit_predict(m, tr, te, list(FEATURES), target="ret") for m in needed}
    snap = data_snapshot_id(data_dir)
    uni = hashlib.sha256("\n".join(sorted(te["code"])).encode()).hexdigest()[:16]
    created = clock()                                            # 학습이 끝난 실제 기록 시각
    if created >= open_utc:
        log(f"{d} 학습 중에 장이 열림 — 소급 기록 금지라 기록하지 않음 (그달은 빠진 달)")
        return {"status": "late", "decision": str(d)}
    recs = []
    for model_id, parts in MODELS:
        p = combine([cache[m] for m in parts]) if len(parts) > 1 else cache[parts[0]]
        rk = p["score"].rank(pct=True)
        for i, row in p.iterrows():
            prob = row["prob"]
            recs.append(ForecastRecord(
                forecast_id=f"{d}:{model_id}:{row['code']}", created_at_utc=created, decision_time_utc=cut,
                data_cutoff_utc=data_cutoff, ticker=row["code"], model_id=model_id, target_definition_id="RET_21D",
                holding_period_sessions=H, predicted_probability=None if pd.isna(prob) else float(prob),
                feature_snapshot_id=snap, feature_hash=list_hash(list(FEATURES)), universe_snapshot_id=uni,
                score=float(row["score"]), rank_pct=float(rk.loc[i])))
    n = ledger.record_many(recs)
    log(f"{d} 판단 기록: 모델 {len(MODELS)}개 × 종목 {len(te):,}개 = {n:,}건 (학습 {len(tr):,}행, "
        f"D−2 종가 보유 {fresh:.0%}, 기록 시각 {created:%Y-%m-%d %H:%M} UTC, 장 시작 {session_start_after(data_cutoff):%Y-%m-%d %H:%M} UTC 전)")
    return {"status": "recorded", "decision": str(d), "records": n, "universe": len(te)}


def dry_run(data_dir, d: date, *, loader=load_price_panel, log=print) -> dict:
    """지난 판단일 d를 실제 데이터로 끝까지 계산해 보되 기록하지 않는다 (실행 시간·데이터 신선도 점검용).
    패널은 d 전날까지만 쓴다 (그때 있었던 자료만)."""
    import time
    t0 = time.time()
    panel = loader(data_dir, START, d)
    panel = panel[panel["date"] < pd.Timestamp(d)]
    tr, te, fresh = build_live(panel, d)
    needed = sorted({m for _, parts in MODELS for m in parts})
    cache = {m: fit_predict(m, tr, te, list(FEATURES), target="ret") for m in needed}
    ok = all(cache[m]["score"].notna().all() for m in needed)
    log(f"점검(기록 안 함) {d}: 학습 {len(tr):,}행, 유니버스 {len(te):,}종목, D−2 종가 보유 {fresh:.0%}, "
        f"점수 {'정상' if ok else '결측 있음'}, {time.time() - t0:.0f}초")
    return {"train": len(tr), "universe": len(te), "fresh": fresh, "scores_ok": ok}


def summary(ledger: ForecastLedger) -> pd.DataFrame:
    """정산된 판단마다 모델별 Rank IC와 상위 10% 평균 − 유니버스 평균 (비용 전, 실현 수익률)."""
    ev = {e["forecast_id"]: e for e in ledger.evaluations()}
    rows = []
    for f in ledger.forecasts():
        e = ev.get(f.forecast_id)
        if e is None or e.get("realized_return") is None or f.score is None:
            continue
        rows.append({"date": f.forecast_id.split(":")[0], "model": f.model_id, "score": f.score,
                     "rank_pct": f.rank_pct, "ret": e["realized_return"]})
    if not rows:
        return pd.DataFrame(columns=["date", "model", "ic", "top_minus_all", "n"])
    df = pd.DataFrame(rows)
    out = []
    for (d, m), g in df.groupby(["date", "model"]):
        ic = float(rank_ic(g.assign(date=d)).iloc[0]) if len(g) > 2 else np.nan
        top = g[g["rank_pct"] > 1 - TOP]["ret"].mean()
        out.append({"date": d, "model": m, "ic": ic, "top_minus_all": float(top - g["ret"].mean()), "n": len(g)})
    return pd.DataFrame(out).sort_values(["model", "date"]).reset_index(drop=True)


def settle(data_dir, now_utc: datetime = None, *, loader=load_price_panel, log=print) -> dict:
    now_utc = now_utc or datetime.now(timezone.utc)
    ledger = ForecastLedger(data_dir)
    if not ledger.pending():
        log("정산할 예측 없음")
    else:
        panel = loader(data_dir, START, now_utc.astimezone(KST).date())
        done = ledger.settle(build_targets(panel, horizons=(H,)), now_utc)
        log(f"정산 {len(done):,}건, 남은 예측 {len(ledger.pending()):,}건")
    s = summary(ledger)
    for m, g in s.groupby("model"):
        log(f"{m}: 정산된 판단 {len(g)}개, Rank IC 평균 {g['ic'].mean():.4f}, "
            f"상위 10% − 유니버스 평균(비용 전) {g['top_minus_all'].mean():+.2%}/판단")
    return {"settled_dates": int(s["date"].nunique()) if len(s) else 0}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default="private_data")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--forecast", action="store_true")
    g.add_argument("--settle", action="store_true")
    g.add_argument("--dry-run", metavar="YYYY-MM-DD", help="지난 판단일을 실제 데이터로 계산만 하고 기록하지 않음")
    args = ap.parse_args(argv)
    if args.dry_run:
        dry_run(args.data_dir, date.fromisoformat(args.dry_run))
        return 0
    if args.check:
        d, first = next_decision(datetime.now(timezone.utc))
        print(f"due={'true' if first else 'false'}")
        print(f"decision={d}")
        return 0
    if args.forecast:
        forecast(args.data_dir)
        return 0
    settle(args.data_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
