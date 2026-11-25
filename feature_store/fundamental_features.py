"""시점 기준(PIT) 재무 피처 — Model A의 PIT 재무 부분 (가이드 5-4, 7-2). 출처: 금융감독원 전자공시시스템(DART).

입력: 공개 data 브랜치의 dart_fin_hist (collectors/opendart_history.py). 보고서마다 백테스트용 가용 시각
(마지막 재무 변경일 다음 세션 08:30, core.availability)을 붙이고, 판단 시각 이전에 쓸 수 있었던 가장 최근 보고서만
종목별 as-of 결합으로 붙인다 (불변조건을 코드에서 검사).

흐름 값(매출·영업이익·순이익)은 최근 12개월(TTM):
  사업보고서: 그 해 금액
  분기·반기: 전년 사업보고서 금액 + 올해 누적 − 전년 같은 기간 누적 (전년 사업보고서도 그 보고서보다 먼저 쓸 수 있었어야 함)
저량 값(자본·자산·부채)은 그 보고서 기말 값.

단위 오류: 원 대신 천원·백만원 단위로 낸 듯한 보고서가 있다 (같은 회사의 다른 보고서보다 자산이 1,000배·100만 배).
보고서 안의 비율(이익률·ROE·부채비율·성장률)은 단위와 무관하지만, 두 보고서를 섞는 TTM과 시가총액 대비 값은 틀어진다.
판단 시각에 알 수 있는 정보만으로 거른다 (회사 전체 이력의 중앙값처럼 미래 보고서를 보는 규칙은 쓰지 않는다):
  TTM       전년 사업보고서와 이 보고서의 자산 규모가 SCALE_JUMP배 넘게 다르면 비움
  bm·ep·sp  자산총계 / 시가총액이 ASSET_MCAP 범위 밖이면 비움 (정상 범위: 성장주 ~0.05, 은행 ~수십; 자산이 없으면 둔다)

피처:
  bm          자본총계 / 시가총액           ep    TTM 순이익 / 시가총액       sp   TTM 매출 / 시가총액
  op_margin   TTM 영업이익 / TTM 매출        roe   TTM 순이익 / 자본총계       leverage 부채총계 / 자산총계
  rev_growth  누적 매출 전년 같은 기간 대비   ni_growth 누적 순이익 전년 대비 (분모는 절댓값)
  fin_age     판단일 − 보고서를 쓸 수 있게 된 날 (일)
시가총액은 시세 피처 행(판단에 쓰는 마지막 시세, 지금 규칙이면 D−2)의 값을 쓴다.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from collectors.opendart_history import SERIES_FIN_HIST
from core.availability import backtest_available_at
from storage.pit_store import read_parquet

FUND_FEATURES = ("bm", "ep", "sp", "op_margin", "roe", "leverage", "rev_growth", "ni_growth", "fin_age")
FUND_VERSION = "fund_v1"
FLOWS = ("revenue", "op_income", "net_income")
SCALE_JUMP = 20.0
ASSET_MCAP = (0.002, 500.0)


def load_fin_hist(public_dir, links: dict = None) -> pd.DataFrame:
    """dart_fin_hist → 보고서(버전)마다 한 행: stock_code, bsns_year, reprt_code, bt_available_at, 계정 값.
    links: 상장폐지 회사의 {고유번호: 과거 종목코드} (feature_store/corp_links.py, 비공개). 종목코드가 빈 행에만 쓴다."""
    links = links or {}
    d = Path(public_dir) / "pit_versions" / SERIES_FIN_HIST
    rows = []
    for p in sorted(d.glob("*.parquet")) if d.exists() else []:
        for r in read_parquet(p):
            if str(getattr(r.quality_status, "value", r.quality_status)) != "valid":
                continue
            attrs = json.loads(r.attributes or "{}")
            v = json.loads(r.value)
            code = r.metric_id.split("|")[-1]
            code = attrs.get("stock_code", "") or links.get(r.entity_id, "")
            row = {"corp_code": r.entity_id, "stock_code": code, "linked": not attrs.get("stock_code") and bool(code),
                   "bsns_year": r.observation_period_start.year,
                   "reprt_code": code, "bt_available_at": pd.Timestamp(backtest_available_at(r))}
            for k, a in v["accounts"].items():
                row[f"{k}_amt"], row[f"{k}_cum"], row[f"{k}_prev_cum"] = a.get("amt"), a.get("cum"), a.get("prev_cum")
                row[f"{k}_prev"] = a.get("prev")
            rows.append(row)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df[df["stock_code"].str.len() == 6]
    return df.sort_values("bt_available_at").reset_index(drop=True)


def report_table(fin: pd.DataFrame) -> pd.DataFrame:
    """보고서마다 TTM 흐름·저량 값·성장률. 전년 사업보고서는 그 보고서보다 먼저(또는 같이) 쓸 수 있었던 것만."""
    if fin.empty:
        return fin
    f = fin.copy()
    for k in FLOWS:
        for s in ("amt", "cum", "prev_cum", "prev"):
            col = f"{k}_{s}"
            if col not in f:
                f[col] = np.nan
            f[col] = pd.to_numeric(f[col], errors="coerce")
        q1 = f["reprt_code"] == "11013"                 # 1분기는 3개월 = 누적
        f.loc[q1 & f[f"{k}_cum"].isna(), f"{k}_cum"] = f.loc[q1, f"{k}_amt"]
        f.loc[q1 & f[f"{k}_prev_cum"].isna(), f"{k}_prev_cum"] = f.loc[q1, f"{k}_prev"]
    annual = f[f["reprt_code"] == "11011"].sort_values("bt_available_at")
    annual = annual.drop_duplicates(["stock_code", "bsns_year"], keep="last")        # 같은 해 여러 버전이면 최신
    ann = annual.set_index(["stock_code", "bsns_year"])
    out = f.copy()
    prev_key = pd.MultiIndex.from_arrays([out["stock_code"], out["bsns_year"] - 1])
    prev = ann.reindex(prev_key)
    prev_ok = (prev["bt_available_at"].to_numpy() <= out["bt_available_at"].to_numpy())
    if "assets_amt" in out and "assets_amt" in prev:
        with np.errstate(invalid="ignore", divide="ignore"):
            jump = (pd.to_numeric(prev["assets_amt"], errors="coerce").to_numpy(dtype=float)
                    / pd.to_numeric(out["assets_amt"], errors="coerce").to_numpy(dtype=float))
            same_scale = (jump < SCALE_JUMP) & (jump > 1 / SCALE_JUMP)
        prev_ok = prev_ok & same_scale                     # 자산이 없으면(NaN) 단위를 확인할 수 없어 TTM도 비움
    is_annual = (out["reprt_code"] == "11011").to_numpy()
    for k in FLOWS:
        ttm_q = prev[f"{k}_amt"].to_numpy() + out[f"{k}_cum"].to_numpy() - out[f"{k}_prev_cum"].to_numpy()
        ttm_q = np.where(prev_ok, ttm_q, np.nan)
        out[f"{k}_ttm"] = np.where(is_annual, out[f"{k}_amt"].to_numpy(), ttm_q)
        cum = np.where(is_annual, out[f"{k}_amt"], out[f"{k}_cum"])
        base = np.where(is_annual, out[f"{k}_prev"].to_numpy(dtype=float), out[f"{k}_prev_cum"].to_numpy(dtype=float))
        out[f"{k}_growth"] = (cum - base) / np.where(np.abs(base) > 0, np.abs(base), np.nan)
    for k in ("equity", "assets", "liabilities"):
        col = f"{k}_amt"
        out[k] = pd.to_numeric(out[col], errors="coerce") if col in out else np.nan
    return out


def fundamentals_at(feats: pd.DataFrame, reports: pd.DataFrame) -> pd.DataFrame:
    """시세 피처가 붙은 판단 표(features_at 결과)에 재무 피처를 붙인다. 종목별 as-of (가용 시각 <= 판단 시각)."""
    out = feats.copy()
    if reports is None or reports.empty:
        for c in FUND_FEATURES:
            out[c] = np.nan
        return out
    cols = ["stock_code", "bt_available_at", "equity", "assets", "liabilities",
            "revenue_ttm", "op_income_ttm", "net_income_ttm", "revenue_growth", "net_income_growth"]
    r = reports[cols].rename(columns={"stock_code": "code", "bt_available_at": "fin_available_at"})
    r = r.dropna(subset=["fin_available_at"]).copy()
    r["fin_available_at"] = pd.to_datetime(r["fin_available_at"], utc=True).astype("datetime64[ns, UTC]")
    r = r.sort_values("fin_available_at")
    left = out.reset_index().rename(columns={"index": "_i"})
    left["_t"] = pd.to_datetime(left["decision_time_utc"], utc=True).astype("datetime64[ns, UTC]")
    left = left.sort_values("_t")
    m = pd.merge_asof(left, r, left_on="_t", right_on="fin_available_at", by="code", direction="backward")
    assert (m["fin_available_at"].isna() | (m["fin_available_at"] <= m["_t"])).all()    # PIT 불변조건
    m = m.set_index("_i").sort_index()
    mcap = np.exp(m["log_mktcap"])
    scale = m["assets"] / mcap
    plausible = scale.isna() | ((scale > ASSET_MCAP[0]) & (scale < ASSET_MCAP[1]))   # 단위 오류로 보이면 비움 (자산이 없으면 확인 불가라 둠)
    out["bm"] = (m["equity"] / mcap).where(plausible)
    out["ep"] = (m["net_income_ttm"] / mcap).where(plausible)
    out["sp"] = (m["revenue_ttm"] / mcap).where(plausible)
    out["fin_unit_suspect"] = m["assets"].notna() & mcap.notna() & ~plausible
    out["op_margin"] = m["op_income_ttm"] / m["revenue_ttm"].where(m["revenue_ttm"] > 0)
    out["roe"] = m["net_income_ttm"] / m["equity"].where(m["equity"] > 0)
    out["leverage"] = m["liabilities"] / m["assets"].where(m["assets"] > 0)
    out["rev_growth"] = m["revenue_growth"]
    out["ni_growth"] = m["net_income_growth"]
    out["fin_age"] = (m["_t"] - m["fin_available_at"]).dt.days
    return out


def unit_suspect_rate(df: pd.DataFrame) -> float:
    """재무가 붙은 행 중 자산/시가총액이 범위 밖이라 bm·ep·sp를 비운 비율."""
    if "fin_unit_suspect" not in df or df["fin_age"].notna().sum() == 0:
        return float("nan")
    return round(float(df.loc[df["fin_age"].notna(), "fin_unit_suspect"].mean()), 5)


def coverage(df: pd.DataFrame) -> dict:
    """연도별 재무 피처가 붙은 비율 (값 없이 비율만)."""
    if "bm" not in df or df.empty:
        return {}
    return {int(y): round(float(g["bm"].notna().mean()), 3) for y, g in df.groupby(df["date"].dt.year)}
