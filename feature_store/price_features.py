"""시점 기준(PIT) 시세 피처 — Model A의 한국 주가·거래량 부분 (가이드 5-4, 7-2).

1. 패널의 각 행(종목·기준일 t)에서 t까지의 행만으로 피처를 계산한다 (롤링 창은 과거 방향만).
2. 판단 세션 D마다 종목별로 "백테스트용 가용 시각 <= D 08:30 KST"인 가장 최근 행의 피처를 붙인다
   (그룹 키가 있는 as-of 결합, 가이드 4-4). 지금 가용 시각 규칙이면 D에 쓰는 마지막 종가는 D−2 세션이다.
3. 수익률은 기준가로 조정한 일간 로그 수익률 log(close / base)라 분할·무상증자에 흔들리지 않는다.

피처 (창은 그 종목의 거래 세션 수):
  mom_5 · mom_21 · mom_63 · mom_126  누적 로그 수익률
  mom_252_21                         12개월 모멘텀에서 최근 1개월을 뺀 값
  rev_1                              마지막 하루 수익률 (단기 반전)
  vol_21 · vol_63                    일간 수익률 표준편차
  dist_high_252                      252세션 최고 조정가 대비 현재 위치 (로그)
  log_adv_21                         21세션 평균 거래대금 (로그)
  turnover_21                        21세션 평균 거래대금 / 시가총액
  volume_z_21                        마지막 거래량 / 21세션 평균 (로그)
  log_mktcap                         시가총액 (로그)
  history                            지금까지 쌓인 세션 수
"""
import numpy as np
import pandas as pd

from core.calendar_align import kr_decision_cutoff

FEATURES = ("mom_5", "mom_21", "mom_63", "mom_126", "mom_252_21", "rev_1", "vol_21", "vol_63", "dist_high_252",
            "log_adv_21", "turnover_21", "volume_z_21", "log_mktcap")
FEATURE_VERSION = "price_v1"


def _roll(g: pd.core.groupby.SeriesGroupBy, w: int, fn: str, minp: int):
    return g.transform(lambda s: getattr(s.rolling(w, min_periods=minp), fn)())


def row_features(panel: pd.DataFrame) -> pd.DataFrame:
    """행(종목·기준일)마다 그날까지의 정보로 만든 피처. 거래가 없는 날도 행은 남긴다 (수익률 0, 거래량 0)."""
    df = panel.sort_values(["code", "date"]).reset_index(drop=True)
    r = np.log(df["close"] / df["base"]).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    g = r.groupby(df["code"])
    out = pd.DataFrame({"date": df["date"], "code": df["code"], "bt_available_at": df["bt_available_at"]})
    for w in (5, 21, 63, 126):
        out[f"mom_{w}"] = _roll(g, w, "sum", w)
    out["mom_252_21"] = _roll(g, 252, "sum", 252) - out["mom_21"]
    out["rev_1"] = r
    out["vol_21"] = _roll(g, 21, "std", 21)
    out["vol_63"] = _roll(g, 63, "std", 63)
    logp = g.cumsum()                                           # 조정 로그 가격 지수 (첫날 = 0)
    out["dist_high_252"] = logp - logp.groupby(df["code"]).transform(lambda s: s.rolling(252, min_periods=252).max())
    value = df["value"].astype(float)
    adv = _roll(value.groupby(df["code"]), 21, "mean", 21)
    out["log_adv_21"] = np.log(adv.where(adv > 0))
    out["turnover_21"] = adv / df["mktcap"].where(df["mktcap"] > 0)
    vol = df["volume"].astype(float)
    vmean = _roll(vol.groupby(df["code"]), 21, "mean", 21)
    out["volume_z_21"] = np.log((vol + 1) / (vmean + 1))
    out["log_mktcap"] = np.log(df["mktcap"].where(df["mktcap"] > 0))
    out["history"] = df.groupby("code").cumcount() + 1
    out["traded"] = df["volume"] > 0
    return out


def decision_grid(panel: pd.DataFrame, every: int = 1) -> pd.DataFrame:
    """판단 세션 D(every 세션마다)와 그 종목 목록. D에 행이 있는(상장된) 종목만."""
    sessions = sorted(panel["date"].unique())[::every]
    g = panel[panel["date"].isin(sessions)][["date", "code"]].copy()
    cut = {d: kr_decision_cutoff(pd.Timestamp(d)) for d in sessions}
    g["decision_time_utc"] = g["date"].map(cut)
    return g


def features_at(panel: pd.DataFrame, grid: pd.DataFrame) -> pd.DataFrame:
    """판단 시각에 쓸 수 있었던 가장 최근 행의 피처를 붙인다 (종목별 as-of, 가용 시각 <= 판단 시각)."""
    rf = row_features(panel).dropna(subset=["bt_available_at"])
    rf = rf.rename(columns={"date": "feature_date"}).sort_values("bt_available_at")
    left = grid.sort_values("decision_time_utc")
    m = pd.merge_asof(left, rf, left_on="decision_time_utc", right_on="bt_available_at", by="code",
                      direction="backward", allow_exact_matches=True)
    assert (m["bt_available_at"].isna() | (m["bt_available_at"] <= m["decision_time_utc"])).all()   # PIT 불변조건
    return m.sort_values(["date", "code"]).reset_index(drop=True)
