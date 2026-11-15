"""예측 타깃 사전 정의 (가이드 7-3). 실험 전에 고정하고, 바꾸려면 새 target_id를 만든다.

판단 시점과 가격 (TradingAgents의 "판단 다음 첫 가격에 진입, 확정 가격으로만 채점" 규칙과 같은 생각):
- 판단 세션 D의 08:30 KST 컷오프에 신호를 낸다. 그때 쓸 수 있는 정보만 쓴다 (백테스트용 가용 시각 <= D 08:30).
- **진입: D의 KRX 시가** (09:00 시가 단일가). 08:30 뒤 처음 실제로 거래할 수 있는 KRX 가격이다.
  넥스트레이드 프리마켓(08:00~08:50) 가격은 쓰지 않는다 (venue = KRX).
- **청산: D+h 세션의 KRX 시가.** 진입과 같은 종류의 가격이라 h세션 동안의 시가→시가 수익률이다.
- 수익률은 기준가로 만든 조정 계수로 분할·무상증자 등을 걸러낸 **가격 수익률**이다 (현금배당 제외).
- 정답(라벨)을 쓸 수 있는 시각 = 청산 세션 시세의 백테스트용 가용 시각. 학습 폴드는 이 시각이 검증 구간의
  판단 시각보다 앞선 표본만 써야 한다 (7-5 Purge·Embargo).

체결 가능성은 정답과 따로 기록한다 (7-10: 예측 정확도와 투자 성과를 분리).
- entry_no_trade: D에 거래가 없음 (거래정지 등) → 진입 불가
- entry_limit_up: D 시가가 상한가 → 매수 체결을 보장할 수 없음 (보수적 가정: 미체결)
- exit_no_trade: D+h에 거래가 없음 → 그 가격에 청산 불가
- exit_limit_down: D+h 시가가 하한가 → 매도 체결을 보장할 수 없음
- no_exit_row: D+h에 그 종목 행이 없음 (상장폐지 등). 정답을 비워 두고 사유를 남긴다 — 버리지 않는다 (생존편향)
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

from evaluation.kr_market_rules import DEFAULT_METHOD, limit_prices_arr


@dataclass(frozen=True)
class TargetDefinition:
    target_id: str
    horizon_sessions: int
    kind: str                       # RET (수익률) | DIR (수익률 > 0)
    entry: str = "KRX_OPEN_D"       # 판단 세션 D의 KRX 시가
    exit: str = "KRX_OPEN_D_PLUS_H"
    venue: str = "KRX"
    return_type: str = "PRICE_ADJUSTED_EX_DIVIDEND"
    decision_cutoff: str = "08:30 KST"
    version: str = "v1"


TARGETS = {t.target_id: t for t in (
    TargetDefinition("RET_1D", 1, "RET"), TargetDefinition("RET_5D", 5, "RET"), TargetDefinition("RET_21D", 21, "RET"),
    TargetDefinition("DIR_1D", 1, "DIR"), TargetDefinition("DIR_5D", 5, "DIR"), TargetDefinition("DIR_21D", 21, "DIR"),
)}


def _limits(df: pd.DataFrame, method: str) -> pd.DataFrame:
    lo, up = limit_prices_arr(df["base"], df["date"], df["market"], method)
    return pd.DataFrame({"limit_down": lo, "limit_up": up}, index=df.index)


def build_targets(panel: pd.DataFrame, horizons=(1, 5, 21), *, limit_method: str = DEFAULT_METHOD) -> pd.DataFrame:
    """패널(feature_store.price_panel) → 판단 세션 D·종목마다 h세션 시가→시가 수익률과 체결 가능성.

    열: date(D), code, h, entry_open, exit_date, exit_open, ret, dir, entry_gap, action_in_window, label_available_at,
        entry_no_trade, entry_limit_up, exit_no_trade, exit_limit_down, no_exit_row, tradable
    시세 값이 들어 있으므로 비공개 저장소 밖으로 내보내지 않는다."""
    df = panel.sort_values(["code", "date"]).reset_index(drop=True).copy()
    df[["limit_down", "limit_up"]] = _limits(df, limit_method)
    df["open_ok"] = df["open"].where(df["open"] > 0)                  # 거래 없는 날 시가는 0
    traded = df["volume"] > 0
    # 종목별 누적 로그 조정 계수: 진입~청산 사이 기업행위를 걸러낸다 (계수가 없는 날은 1로 보고 표시)
    logf = np.log(df["factor"].where(df["factor"] > 0)).fillna(0.0)
    df["cumlog"] = logf.groupby(df["code"]).cumsum()

    sessions = pd.Index(sorted(df["date"].unique()))
    pos = pd.Series(np.arange(len(sessions)), index=sessions)
    key = df.set_index(["code", "date"])
    out = []
    for h in horizons:
        exit_pos = df["date"].map(pos) + h
        valid = exit_pos < len(sessions)
        exit_date = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
        exit_date[valid] = sessions[exit_pos[valid].astype(int)].values
        idx = pd.MultiIndex.from_arrays([df["code"], exit_date])
        ex = key.reindex(idx)
        t = pd.DataFrame({
            "date": df["date"], "code": df["code"], "h": h, "entry_open": df["open_ok"],
            "exit_date": exit_date, "exit_open": ex["open"].where(ex["open"] > 0).values,
            "label_available_at": pd.Series(ex["bt_available_at"].array, index=df.index)})
        growth = np.exp(ex["cumlog"].values - df["cumlog"].values)
        t["ret"] = t["exit_open"] / (t["entry_open"] * growth) - 1
        t["entry_gap"] = (df["open_ok"] / df["base"] - 1).values                 # 기준가(조정된 전일 종가) 대비 시가
        t["action_in_window"] = np.abs(ex["cumlog"].values - df["cumlog"].values) > 1e-12
        t["dir"] = (t["ret"] > 0).astype("float").where(t["ret"].notna())
        t["no_exit_row"] = valid.values & ex["close"].isna().values
        t["entry_no_trade"] = ~traded.values | t["entry_open"].isna().values
        t["entry_limit_up"] = (df["open_ok"] >= df["limit_up"]).values
        t["exit_no_trade"] = ~(ex["volume"].fillna(0).values > 0) & ~t["no_exit_row"].values & valid.values
        t["exit_limit_down"] = (ex["open"].values <= ex["limit_down"].values) & (ex["open"].values > 0)
        t["tradable"] = ~(t["entry_no_trade"] | t["entry_limit_up"] | t["exit_no_trade"] | t["exit_limit_down"]
                          | t["no_exit_row"]) & t["ret"].notna()
        out.append(t[valid.values])
    return pd.concat(out, ignore_index=True)


def summarize(targets: pd.DataFrame) -> dict:
    """공개 로그용 요약 (건수·비율만)."""
    res = {}
    for h, g in targets.groupby("h"):
        res[int(h)] = {"rows": len(g), "labeled": int(g["ret"].notna().sum()), "tradable": int(g["tradable"].sum()),
                       "entry_no_trade": int(g["entry_no_trade"].sum()), "entry_limit_up": int(g["entry_limit_up"].sum()),
                       "exit_no_trade": int(g["exit_no_trade"].sum()), "exit_limit_down": int(g["exit_limit_down"].sum()),
                       "no_exit_row": int(g["no_exit_row"].sum()),
                       "up_rate": None if g["dir"].notna().sum() == 0 else round(float(g["dir"].mean()), 4)}
    return res
