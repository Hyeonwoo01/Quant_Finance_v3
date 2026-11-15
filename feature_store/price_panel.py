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

열: date, code, (name: include_names=True일 때만), market, open, high, low, close, volume, value, shares, mktcap,
    base, chg, factor, ret_cc, bt_available_at
메모리: 하루 파일씩 읽어 숫자 열만 남긴다. 16년치(약 940만 행)를 문자열째 한꺼번에 올리면 실행기(16GB)가 멈춘다.
"""
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
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


_CUTOFF_CACHE: dict = {}


def _cutoff(t) -> pd.Timestamp:
    if t not in _CUTOFF_CACHE:
        _CUTOFF_CACHE[t] = first_kr_cutoff_at_or_after(t)
    return _CUTOFF_CACHE[t]


def _day_latest(path: Path, series_id: str, as_of: Optional[pd.Timestamp]) -> tuple[dict, dict]:
    """하루 파일 → (열 사전, {entity_id: (백테스트용 가용 시각, 행 번호)}). 같은 종목의 여러 버전 중 as_of에
    쓸 수 있었던 최신 버전만 고른다. 백테스트용 가용 시각은 core.availability.backtest_available_at과 같은 규칙."""
    import pyarrow.parquet as pq
    t = pq.read_table(path, columns=_COLS).to_pydict()
    immutable = series_id in IMMUTABLE_MARKET_SERIES
    best = {}
    for i, ent in enumerate(t["entity_id"]):
        if t["quality_status"][i] != "valid":
            continue
        if t["license_contract_id"][i] != KRX_OPENAPI_STOCK.contract_id:
            raise PermissionDenied(f"예상하지 못한 계약의 레코드: {t['license_contract_id'][i]}")
        prov = t["provider_available_at_utc"][i]
        if immutable and t["supersedes_revision_id"][i] is None and prov is not None:
            bt = _cutoff(prov)
        else:
            bt = pd.Timestamp(t["available_at_utc"][i])
        if as_of is not None and bt > as_of:
            continue
        key = (bt, t["ingested_at_utc"][i])
        if ent not in best or key >= best[ent][0]:
            best[ent] = (key, i)
    return t, {e: (k[0], i) for e, (k, i) in best.items()}


def _day_frame(price_path: Path, base_path: Optional[Path], as_of, include_names: bool) -> pd.DataFrame:
    """하루치 시세 + 기준가 → 숫자 열만 남긴 작은 표 (16년치를 한꺼번에 문자열로 올리지 않기 위해 하루씩 줄인다)."""
    pt, pbest = _day_latest(price_path, SERIES_KRX_PRICE, as_of)
    bt_, bbest = _day_latest(base_path, SERIES_KRX_BASE, as_of) if base_path is not None else ({}, {})
    ents = list(pbest)
    rows = {c: np.full(len(ents), np.nan) for c in PRICE_COLS + ["base", "chg"]}
    avail, market, name = [], [], []
    for j, ent in enumerate(ents):
        a, i = pbest[ent]
        v = json.loads(json.loads(pt["value_json"][i]))
        for c in PRICE_COLS:
            x = v.get(c)
            if x is not None:
                rows[c][j] = x
        attrs = json.loads(pt["attributes"][i] or "{}")
        market.append(attrs.get("market"))
        if include_names:
            name.append(attrs.get("name"))
        if ent in bbest:                                  # 기준가가 아직 쓸 수 없으면 계수도 쓰지 않는다
            ba, bi = bbest[ent]
            bv = json.loads(json.loads(bt_["value_json"][bi]))
            rows["base"][j], rows["chg"][j] = bv.get("base", np.nan), bv.get("chg", np.nan)
            a = max(a, ba)
        avail.append(a)
    day = pd.Timestamp(pt["observation_period_start"][0]) if ents else None
    df = pd.DataFrame({"date": day, "code": [e.split(":")[-1] for e in ents], "market": market, **rows,
                       "bt_available_at": pd.to_datetime(avail, utc=True) if avail else pd.Series(dtype="datetime64[ns, UTC]")})
    if include_names:
        df["name"] = name
    return df


def load_price_panel(data_dir, start: date, end: date, *, as_of_utc: Optional[datetime] = None,
                     today: Optional[date] = None, registry: Optional[ContractRegistry] = None,
                     include_names: bool = False) -> pd.DataFrame:
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
    frames = [_day_frame(prices[d], bases.get(d), as_of, include_names) for d in days]
    frames = [f for f in frames if len(f)]
    cols = ["date", "code", *(["name"] if include_names else []), "market", *PRICE_COLS, "base", "chg", "factor",
            "ret_cc", "bt_available_at"]
    if not frames:
        return pd.DataFrame(columns=cols)
    out = pd.concat(frames, ignore_index=True)
    del frames
    out["market"] = out["market"].astype("category")
    out = out.sort_values(["code", "date"], ignore_index=True)
    prev_close = out.groupby("code")["close"].shift(1)
    out["factor"] = out["base"] / prev_close
    out["ret_cc"] = out["close"] / out["base"] - 1
    out = out[out["date"] >= pd.Timestamp(start)]
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
