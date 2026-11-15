"""학습·백테스트용 한국 주식 일별 시세 패널 (KRX Open API, 비공개 저장소).

    panel = load_price_panel("private_data", date(2015, 1, 1), date(2025, 12, 30), as_of_utc=...)

- **권한 먼저:** 계약 `krx_openapi_stock_v1`의 ML 학습 승인과 인증키 이용기간(`license_ok`)을 확인한 뒤에만 읽는다.
  이용기간이 지나면 읽지 않는다 (약관 제11조 ③).
- **시점:** 값마다 백테스트용 가용 시각(`core/availability.py`)을 붙인다. `as_of_utc`를 주면 그 시각에 쓸 수 있었던
  값만, 같은 종목·기준일의 여러 버전 중 그때 최신 버전만 남긴다.
- **수정주가:** 시세는 수정하지 않은 원래 가격이다. 대신 그날의 조정 계수 `factor` = 기준가 ÷ 전 거래일 종가를 준다.
  기업행위(분할·병합·무상증자·권리락)가 없는 날은 정확히 1이다. 수익률은 계수로 나눠 계산한다
  (예: 조정 일간 수익률 = close / base − 1). 현금배당은 기준가에 반영되지 않으므로 가격 수익률이다.
- 시세 값은 출력하지 않는다 (공개 Actions 로그). 반환한 표는 비공개 저장소 밖으로 내보내지 않는다.

열: date, code, name, market, open, high, low, close, volume, value, shares, mktcap, base, chg, factor, ret_cc,
    bt_available_at
"""
import io
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from collectors import krx_openapi
from collectors.krx_openapi import SERIES_KRX_BASE, SERIES_KRX_PRICE
from core.availability import IMMUTABLE_MARKET_SERIES
from core.calendar_align import first_kr_cutoff_at_or_after
from core.data_contracts import ContractRegistry, PermissionDenied, Purpose
from core.source_registry import KRX_OPENAPI_STOCK, default_registry

KST = ZoneInfo("Asia/Seoul")
_COLS = ["entity_id", "revision_id", "supersedes_revision_id", "value_json", "attributes", "available_at_utc",
         "provider_available_at_utc", "ingested_at_utc", "quality_status", "license_contract_id",
         "observation_period_start"]
PRICE_COLS = ["open", "high", "low", "close", "volume", "value", "shares", "mktcap"]


def _day_files(data_dir: Path, series_id: str) -> dict:
    d = data_dir / "pit_versions" / series_id
    return {date.fromisoformat(p.stem): p for p in d.glob("*.parquet")} if d.exists() else {}


def _read(files: list) -> pd.DataFrame:
    import pyarrow as pa
    import pyarrow.parquet as pq
    if not files:
        return pd.DataFrame(columns=_COLS)
    return pa.concat_tables([pq.read_table(f, columns=_COLS) for f in files]).to_pandas()


def _bt_available(df: pd.DataFrame, series_id: str) -> pd.Series:
    """core.availability.backtest_available_at을 표 전체에 적용 (공급자 가용 시각은 기준일마다 같아 한 번씩만 계산)."""
    avail = pd.to_datetime(df["available_at_utc"], utc=True)
    if series_id not in IMMUTABLE_MARKET_SERIES:
        return avail
    prov = pd.to_datetime(df["provider_available_at_utc"], utc=True)
    cut = {t: first_kr_cutoff_at_or_after(t) for t in prov.dropna().unique()}
    rule = prov.map(cut)
    original = df["supersedes_revision_id"].isna() & prov.notna()
    return rule.where(original, avail)


def _latest(df: pd.DataFrame, series_id: str, as_of: Optional[pd.Timestamp]) -> pd.DataFrame:
    df = df[df["quality_status"] == "valid"].copy()
    df["bt_available_at"] = _bt_available(df, series_id)
    if as_of is not None:
        df = df[df["bt_available_at"] <= as_of]
    df["ingested_at_utc"] = pd.to_datetime(df["ingested_at_utc"], utc=True)
    df = df.sort_values(["entity_id", "observation_period_start", "bt_available_at", "ingested_at_utc"])
    return df.drop_duplicates(["entity_id", "observation_period_start"], keep="last")


def _json_frame(col: pd.Series, *, wrapped: bool = False) -> pd.DataFrame:
    """JSON 문자열 열 → 표. wrapped: Parquet의 value_json처럼 값(JSON 문자열)을 한 번 더 JSON으로 감싼 열."""
    if col.empty:
        return pd.DataFrame(index=col.index)
    texts = col.map(json.loads) if wrapped else col
    out = pd.read_json(io.StringIO("\n".join(texts.tolist())), lines=True, dtype=False)
    out.index = col.index
    return out


def load_price_panel(data_dir, start: date, end: date, *, as_of_utc: Optional[datetime] = None,
                     today: Optional[date] = None, registry: Optional[ContractRegistry] = None) -> pd.DataFrame:
    data_dir = Path(data_dir)
    registry = registry or default_registry()
    registry.require(KRX_OPENAPI_STOCK.contract_id, Purpose.ML_TRAINING)
    today = today or datetime.now(timezone.utc).astimezone(KST).date()
    if not krx_openapi.license_ok(data_dir, today):
        raise PermissionDenied("KRX Open API 인증키 이용기간 기록이 없거나 지났음 — 학습에 쓰지 않는다 (약관 제11조 ③)")
    as_of = pd.Timestamp(as_of_utc).tz_convert("UTC") if as_of_utc is not None else None

    prices, bases = _day_files(data_dir, SERIES_KRX_PRICE), _day_files(data_dir, SERIES_KRX_BASE)
    days = sorted(d for d in prices if start <= d <= end)
    before = [d for d in prices if d < start]
    if before:
        days.insert(0, max(before))                      # 첫날의 조정 계수에 필요한 전 거래일 종가
    p = _latest(_read([prices[d] for d in days]), SERIES_KRX_PRICE, as_of)
    b = _latest(_read([bases[d] for d in days if d in bases]), SERIES_KRX_BASE, as_of)
    for df in (p, b):
        bad = set(df["license_contract_id"]) - {KRX_OPENAPI_STOCK.contract_id}
        if bad:
            raise PermissionDenied(f"예상하지 못한 계약의 레코드: {sorted(bad)}")

    out = pd.concat([p[["entity_id", "observation_period_start", "bt_available_at"]],
                     _json_frame(p["value_json"], wrapped=True)[PRICE_COLS] if len(p) else pd.DataFrame(columns=PRICE_COLS),
                     _json_frame(p["attributes"]).reindex(columns=["name", "market"])], axis=1)
    bv = _json_frame(b["value_json"], wrapped=True).reindex(columns=["base", "chg"])
    bv["entity_id"], bv["observation_period_start"] = b["entity_id"], b["observation_period_start"]
    bv["base_available_at"] = b["bt_available_at"]
    out = out.merge(bv, on=["entity_id", "observation_period_start"], how="left")
    # 기준가가 아직 쓸 수 없는 시각이면 계수도 쓰지 않는다 (as_of 기준 둘 중 늦은 쪽)
    out["bt_available_at"] = out[["bt_available_at", "base_available_at"]].max(axis=1)
    out = out.drop(columns="base_available_at")
    out["code"] = out["entity_id"].str.split(":").str[-1]
    out["date"] = pd.to_datetime(out["observation_period_start"])
    out = out.sort_values(["code", "date"], ignore_index=True)
    for c in PRICE_COLS + ["base", "chg"]:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    prev_close = out.groupby("code")["close"].shift(1)
    out["factor"] = out["base"] / prev_close
    out["ret_cc"] = out["close"] / out["base"] - 1
    out = out[out["date"] >= pd.Timestamp(start)]
    cols = ["date", "code", "name", "market", *PRICE_COLS, "base", "chg", "factor", "ret_cc", "bt_available_at"]
    return out[cols].reset_index(drop=True)


def corporate_action_days(panel: pd.DataFrame, tol: float = 1e-9) -> pd.DataFrame:
    """조정 계수가 1이 아닌 날 (분할·병합·무상증자·권리락 등 후보). 값 대신 종목·날짜·계수만 돌려준다."""
    m = panel["factor"].notna() & ((panel["factor"] - 1).abs() > tol)
    return panel.loc[m, ["date", "code", "factor"]].reset_index(drop=True)


def describe(panel: pd.DataFrame) -> dict:
    """공개 로그에 남겨도 되는 요약 (건수·기간·결측만, 시세 값 없음)."""
    if panel.empty:
        return {"rows": 0}
    return {"rows": len(panel), "codes": int(panel["code"].nunique()), "days": int(panel["date"].nunique()),
            "first": str(panel["date"].min().date()), "last": str(panel["date"].max().date()),
            "missing_base": int(panel["base"].isna().sum()), "missing_factor": int(panel["factor"].isna().sum()),
            "action_days": len(corporate_action_days(panel))}


if __name__ == "__main__":
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="학습용 시세 패널 요약 (값은 출력하지 않음)")
    ap.add_argument("--data-dir", default="private_data")
    ap.add_argument("--start", default="2010-01-01")
    ap.add_argument("--end", default=str(date.today()))
    a = ap.parse_args()
    print(json.dumps(describe(load_price_panel(a.data_dir, date.fromisoformat(a.start), date.fromisoformat(a.end))),
                     ensure_ascii=False))
    sys.exit(0)
