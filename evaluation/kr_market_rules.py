"""한국 주식시장 규칙: 호가가격단위·가격제한폭 (가이드 8-2).

- **호가가격단위:** 날짜별 표를 쓴다. 2023-01-25 개편 전에는 유가증권·코스닥 표가 달랐고, 개편 뒤에는 시장 구분 없이
  하나의 표다 (2022-11-01 한국거래소 시행세칙 개정 예고, 2023-01-25 시행 — 언론 보도로 확인). 개편 뒤 표는 2차 자료의
  7구간 정리를 썼다. 보도는 "10개 구간"이라고 해 차이가 있으므로 실제 시세 검증(check_against_data)으로 확인한다.
- **가격제한폭:** 기준가격의 ±15% (2015-06-15 전), ±30% (2015-06-15부터). 신규상장 첫날·정리매매 등은 예외라
  여기서 계산하지 않는다 (limit_prices가 None을 돌려주는 경우).
- **상·하한가 금액:** 제한폭 = 기준가격 × 비율을 호가단위 미만에서 자른다. 어느 가격대의 호가단위로 자르는지는
  자료마다 다르다 (가이드 8-2의 지적). 그래서 두 방식을 모두 두고 실제 시세로 검증한 쪽을 기본으로 쓴다:
    BASE_TICK  — 제한폭을 기준가격 가격대의 호가단위로 자른다
    LIMIT_TICK — 기준가격 ± 제한폭을 계산한 뒤 그 가격 가격대의 호가단위로 자른다
    DOUBLE     — BASE_TICK으로 자른 뒤, 결과가 주문 가능한 가격이 되도록 그 가격대 호가단위로 한 번 더 자른다
                 (상한가는 내림, 하한가는 올림). BASE_TICK만으로는 가격대 경계에서 주문할 수 없는 가격이 나온다
                 (예: 기준가 9,980원 → 12,970원, 그 가격대 호가단위는 50원)
  검증: `python -m evaluation.kr_market_rules --data-dir private_data` (값 없이 위반·일치 건수만 출력).

이 모듈의 판단은 일별 OHLCV로 하는 **보수적 체결 가정**이다. 상한가에 도달했다고 반드시 못 사는 것은 아니고
(대기열·잔량에 따라 다름), 도달하지 않았다고 반드시 체결되는 것도 아니다 (8-2).
"""
import math
from datetime import date
from typing import Optional

TICK_REFORM = date(2023, 1, 25)
LIMIT_30 = date(2015, 6, 15)
BASE_TICK, LIMIT_TICK, DOUBLE = "BASE_TICK", "LIMIT_TICK", "DOUBLE"
DEFAULT_METHOD = DOUBLE  # 2026-11-15 rulecheck: 세 시기 모두 가장 잘 맞음 (MASTER_GUIDE 8-2 구현 노트)

# (상한 미만 가격, 호가단위) — 마지막 구간은 그 이상 전부
_TICKS_KOSPI_OLD = ((1_000, 1), (5_000, 5), (10_000, 10), (50_000, 50), (100_000, 100), (500_000, 500), (math.inf, 1_000))
_TICKS_KOSDAQ_OLD = ((1_000, 1), (5_000, 5), (10_000, 10), (50_000, 50), (math.inf, 100))
_TICKS_UNIFIED = ((2_000, 1), (5_000, 5), (20_000, 10), (50_000, 50), (200_000, 100), (500_000, 500), (math.inf, 1_000))


def tick_size(price: float, day: date, market: str) -> int:
    if day >= TICK_REFORM:
        table = _TICKS_UNIFIED
    elif market.upper() == "KOSDAQ":
        table = _TICKS_KOSDAQ_OLD
    else:
        table = _TICKS_KOSPI_OLD
    for upper, tick in table:
        if price < upper:
            return tick
    raise ValueError(price)


def limit_rate(day: date) -> float:
    return 0.30 if day >= LIMIT_30 else 0.15


def limit_prices(base: Optional[float], day: date, market: str, method: str = DEFAULT_METHOD) -> Optional[tuple]:
    """(하한가, 상한가). 기준가격이 없거나 0 이하면 None."""
    if base is None or not base > 0 or (isinstance(base, float) and math.isnan(base)):
        return None
    base = int(base)
    raw = base * limit_rate(day)
    if method in (BASE_TICK, DOUBLE):
        t = tick_size(base, day, market)
        width = int(raw // t) * t
        lo, up = base - width, base + width
        if method == BASE_TICK:
            return lo, up
        tu, tl = tick_size(up, day, market), tick_size(lo, day, market)
        return int(math.ceil(lo / tl) * tl), int(up // tu) * tu
    if method == LIMIT_TICK:
        up, lo = math.floor(base + raw), math.ceil(base - raw)
        tu, tl = tick_size(up, day, market), tick_size(lo, day, market)
        return int(math.ceil(lo / tl) * tl), int(up // tu) * tu
    raise ValueError(f"알 수 없는 방식: {method}")


def _tick_arr(price, day, kosdaq):
    """tick_size의 벡터판 (numpy 배열). day: datetime64[D] 배열, kosdaq: bool 배열."""
    import numpy as np
    price = np.asarray(price, dtype=float)
    out = np.empty(len(price), dtype=float)
    new = day >= np.datetime64(TICK_REFORM)
    for mask, table in ((new, _TICKS_UNIFIED), (~new & kosdaq, _TICKS_KOSDAQ_OLD), (~new & ~kosdaq, _TICKS_KOSPI_OLD)):
        uppers = np.array([u for u, _ in table])
        ticks = np.array([t for _, t in table], dtype=float)
        idx = np.searchsorted(uppers, price[mask], side="right")
        out[mask] = ticks[np.minimum(idx, len(ticks) - 1)]
    return out


def limit_prices_arr(base, dates, markets, method: str = DEFAULT_METHOD):
    """limit_prices의 벡터판 → (하한가, 상한가) 배열. 기준가격이 없거나 0 이하면 NaN."""
    import numpy as np
    import pandas as pd
    base = np.floor(np.asarray(base, dtype=float))
    day = pd.to_datetime(pd.Series(dates)).to_numpy().astype("datetime64[D]")
    kosdaq = np.asarray(pd.Series(markets).astype(str).str.upper() == "KOSDAQ")
    ok = np.isfinite(base) & (base > 0)
    b = np.where(ok, base, 1.0)
    rate = np.where(day >= np.datetime64(LIMIT_30), 0.30, 0.15)
    raw = b * rate
    if method in (BASE_TICK, DOUBLE):
        t = _tick_arr(b, day, kosdaq)
        width = np.floor(raw / t) * t
        lo, up = b - width, b + width
        if method == DOUBLE:
            tu, tl = _tick_arr(up, day, kosdaq), _tick_arr(lo, day, kosdaq)
            lo, up = np.ceil(lo / tl) * tl, np.floor(up / tu) * tu
    elif method == LIMIT_TICK:
        up, lo = np.floor(b + raw), np.ceil(b - raw)
        tu, tl = _tick_arr(up, day, kosdaq), _tick_arr(lo, day, kosdaq)
        lo, up = np.ceil(lo / tl) * tl, np.floor(up / tu) * tu
    else:
        raise ValueError(f"알 수 없는 방식: {method}")
    return np.where(ok, lo, np.nan), np.where(ok, up, np.nan)


def check_against_data(panel, methods=(BASE_TICK, LIMIT_TICK, DOUBLE)) -> dict:
    """실제 시세로 두 방식을 검증한다 (값 없이 건수만).

    - over/under: 고가 > 상한가 또는 저가 < 하한가인 행 (규칙이 틀렸거나 예외 종목)
    - close_off_tick: 종가가 그날 호가단위 표의 배수가 아닌 행 (표가 틀렸으면 많이 나온다)
    - limit_close: 종가가 기준가 대비 (제한폭 − 1%p) 이상 오르거나 내린 행 중 종가가 계산한 상·하한가와 정확히 같은 비율
    신규상장 첫날(전 거래일 종가 없음)과 거래가 없는 행은 뺀다."""
    import pandas as pd
    import numpy as np
    df = panel[(panel["volume"] > 0) & panel["factor"].notna() & (panel["base"] > 0)]
    day = pd.to_datetime(df["date"]).to_numpy().astype("datetime64[D]")
    kosdaq = np.asarray(df["market"].astype(str).str.upper() == "KOSDAQ")
    close = df["close"].to_numpy(dtype=float)
    # 호가단위 표 검증: 거래가 있었던 날의 종가는 그날 표의 호가단위 배수여야 한다
    off = np.mod(close, _tick_arr(close, day, kosdaq)) != 0
    out = {"rows": int(len(df)), "close_off_tick": int(off.sum())}
    if off.any():            # 어긋난 행이 어느 시장·가격대에 몰려 있는지 (호가단위 표 진단용, 값 대신 구간만)
        new = day >= np.datetime64(TICK_REFORM)
        bands = []
        for i in np.flatnonzero(off):
            table = _TICKS_UNIFIED if new[i] else (_TICKS_KOSDAQ_OLD if kosdaq[i] else _TICKS_KOSPI_OLD)
            lo = 0
            for upper, tick in table:
                if close[i] < upper:
                    break
                lo = upper
            bands.append(f"{'KOSDAQ' if kosdaq[i] else 'KOSPI'} {'개편후' if new[i] else '개편전'} {lo:,.0f}원~ (표 호가단위 {tick})")
        out["off_tick_by_band"] = dict(pd.Series(bands).value_counts().head(15).items())
    base = df["base"].to_numpy(dtype=float)
    rate = np.where(day >= np.datetime64(LIMIT_30), 0.30, 0.15)
    near_up = close / base - 1 >= rate - 0.01
    near_dn = close / base - 1 <= -(rate - 0.01)
    high, low = df["high"].to_numpy(dtype=float), df["low"].to_numpy(dtype=float)
    for m in methods:
        lo, up = limit_prices_arr(base, df["date"], df["market"], m)
        out[m] = {"over": int((high > up).sum()), "under": int((low < lo).sum()),
                  "near_limit_close": int(near_up.sum() + near_dn.sum()),
                  "exact_limit_close": int((near_up & (close == up)).sum() + (near_dn & (close == lo)).sum())}
    return out


if __name__ == "__main__":
    import argparse
    import json
    from datetime import date as _d

    from feature_store.price_panel import load_price_panel
    ap = argparse.ArgumentParser(description="가격제한폭 계산 방식을 실제 시세로 검증 (값은 출력하지 않음)")
    ap.add_argument("--data-dir", default="private_data")
    ap.add_argument("--start", default="2010-01-01")
    ap.add_argument("--end", default=str(_d.today()))
    a = ap.parse_args()
    panel = load_price_panel(a.data_dir, _d.fromisoformat(a.start), _d.fromisoformat(a.end))
    res = {"all": check_against_data(panel)}
    for name, (s, e) in {"2010-2015-06": ("2010-01-01", "2015-06-14"), "2015-06-2023-01": ("2015-06-15", "2023-01-24"),
                         "2023-01~": ("2023-01-25", "2100-01-01")}.items():
        part = panel[(panel["date"] >= s) & (panel["date"] <= e)]
        res[name] = check_against_data(part)
    print(json.dumps(res, ensure_ascii=False, indent=1))
