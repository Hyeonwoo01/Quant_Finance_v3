"""Long-only 상위 분위 포트폴리오 평가 (가이드 7-8 전략 성과, 8-4 Long-only/Cash, 8-5 시나리오).

판단일마다 점수 상위 top 비율 종목을 같은 비중으로 산다 (시가 진입, h세션 뒤 시가 청산, 판단 간격 = h).
- 진입일 거래가 없거나 시가가 상한가인 종목은 체결되지 않은 것으로 보고 그 비중은 현금 (수익 0).
- 용량: 종목당 주문 금액(notional / 종목 수)이 21세션 평균 거래대금 × max_participation을 넘으면 넘는 만큼 현금.
- 청산일 행이 없으면(상장폐지 등) 그 전 마지막 종가로 계산한 수익률을 쓰고, 그것도 없으면 −100%로 본다.
- 청산일에 거래가 없거나 하한가여도 그 시가 수익률을 쓴다 (실제로는 더 늦게 팔았을 수 있다 — 건수를 함께 보고).
- 비용: 직전 보유와 겹치는 종목은 비중 차이만큼만 사고판다. 매수: 수수료+슬리피지, 매도: 수수료+슬리피지+세금.
- 기준(벤치마크): 같은 유니버스에서 진입 가능한 종목 전체의 같은 비중 평균 (비용 없음).
공매도는 쓰지 않는다 (한국 개인 공매도 제약). 결과는 SIMULATED 체결 가정이다.
"""
import math

import numpy as np
import pandas as pd

from evaluation.costs import SCENARIOS, CostScenario, sell_tax, slippage


def _realized(df: pd.DataFrame) -> pd.Series:
    r = df["ret"].copy()
    nx = df["no_exit_row"].fillna(False).astype(bool)
    r[nx] = df.loc[nx, "ret_delist"].fillna(-1.0)
    return r


def long_only(pred: pd.DataFrame, *, h: int, top: float = 0.1, keep: float = None, notional: float = 1e8,
              scenario: CostScenario = SCENARIOS[0]) -> pd.DataFrame:
    """판단일별 결과 표: date, n, filled, cash, gross, cost, net, bench, excess, turnover.

    keep (교체 완충): 이미 들고 있는 종목은 점수가 상위 keep 비율 안에 있으면 계속 들고 간다 (새로 사는 것은 상위
    top 안에서만). keep=None이면 top과 같다 (완충 없음). 회전율을 줄여 비용을 낮추는 장치다."""
    keep = top if keep is None else keep
    rows, prev = [], pd.Series(dtype=float)
    for d, g in pred.sort_values("date").groupby("date"):
        g = g.dropna(subset=["score"])
        if len(g) < 20:
            continue
        pct = g["score"].rank(pct=True, method="first")
        held = g["code"].isin(prev.index[prev > 0])
        pick = g[(pct > 1 - top) | (held & (pct > 1 - keep))]
        n = len(pick)
        w_target = 1.0 / n
        was_held = pick["code"].isin(prev.index[prev > 0]).to_numpy()
        fillable = ~(pick["entry_no_trade"].fillna(True).astype(bool) | pick["entry_limit_up"].fillna(False).astype(bool))
        adv = np.exp(pick["log_adv_21"].to_numpy(dtype=float))
        cap = np.where(np.isfinite(adv), adv * scenario.max_participation / (notional * w_target), 0.0)
        frac = np.where(fillable, np.minimum(1.0, cap), 0.0)
        frac = np.where(was_held, 1.0, frac)            # 이미 들고 있는 종목은 새로 살 필요가 없다
        w = pd.Series(w_target * frac, index=pick["code"].values)
        r = _realized(pick).to_numpy(dtype=float)
        r = np.where(np.isfinite(r), r, 0.0)
        gross = float((w.values * r).sum())
        # 비용: 직전 보유(청산 시점 비중)와의 차이만 거래한다
        codes = w.index.union(prev.index)
        w_new, w_old = w.reindex(codes, fill_value=0.0), prev.reindex(codes, fill_value=0.0)
        buy, sell = (w_new - w_old).clip(lower=0), (w_old - w_new).clip(lower=0)
        px = pick.set_index("code")["entry_open"].reindex(codes)
        mk = pick.set_index("code")["market"].astype(str).reindex(codes).fillna("KOSPI")
        slip = pd.Series(slippage(px.fillna(0).to_numpy(), [d] * len(codes), mk.to_numpy(), scenario), index=codes)
        slip = slip.fillna(slip.median() if slip.notna().any() else 0.003)
        tax = float(sell_tax([d])[0]) + scenario.tax_shift
        entry_cost = float((buy * (scenario.commission + slip)).sum())
        exit_cost = float((sell * (scenario.commission + slip + tax)).sum())
        # 이번 보유의 청산은 다음 판단일에 계산한다: 끝까지 들고 가는 비중 = w × (1 + r)을 정규화하지 않고 그대로 둔다
        prev = pd.Series(w.values * (1 + r), index=w.index) / max(1.0 + gross, 1e-9)
        bench_pool = g[~(g["entry_no_trade"].fillna(True).astype(bool) | g["entry_limit_up"].fillna(False).astype(bool))]
        bench = float(np.nanmean(np.where(np.isfinite(_realized(bench_pool)), _realized(bench_pool), np.nan)))
        net = gross - entry_cost - exit_cost
        rows.append({"date": d, "n": n, "filled": float(frac.mean()), "cash": float(1 - w.sum()), "gross": gross,
                     "cost": entry_cost + exit_cost, "net": net, "bench": bench, "excess": net - bench,
                     "turnover": float(buy.sum() + sell.sum()) / 2,
                     "exit_issue": int((pick["exit_no_trade"].fillna(False) | pick["exit_limit_down"].fillna(False)).sum()),
                     "delisted": int(pick["no_exit_row"].fillna(False).sum())})
    return pd.DataFrame(rows)


def summarize(res: pd.DataFrame, *, h: int) -> dict:
    if res.empty:
        return {}
    per_year = 252 / h

    def ann(x):
        return float((1 + x).prod() ** (per_year / len(x)) - 1) if len(x) else float("nan")

    def sharpe(x):
        return float(x.mean() / x.std() * math.sqrt(per_year)) if x.std() > 0 else float("nan")

    def mdd(x):
        c = (1 + x).cumprod()
        return float((c / c.cummax() - 1).min())
    out = {"periods": len(res), "avg_names": float(res["n"].mean()), "ann_net": ann(res["net"]), "ann_gross": ann(res["gross"]), "ann_bench": ann(res["bench"]),
           "ann_excess": ann(res["net"]) - ann(res["bench"]), "sharpe_net": sharpe(res["net"]),
           "sharpe_excess": sharpe(res["excess"]), "mdd_net": mdd(res["net"]),
           "avg_turnover": float(res["turnover"].mean()), "avg_cost": float(res["cost"].mean()),
           "avg_cash": float(res["cash"].mean()), "exit_issue_share": float(res["exit_issue"].sum() / res["n"].sum()),
           "delisted": int(res["delisted"].sum())}
    out["excess_by_year"] = {int(y): round(ann(g["net"]) - ann(g["bench"]), 4)
                             for y, g in res.groupby(pd.to_datetime(res["date"]).dt.year)}
    return out
