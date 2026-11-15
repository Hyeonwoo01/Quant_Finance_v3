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
DEFAULT_METHOD = DOUBLE          # 실제 시세 검증 결과로 정한다 (MASTER_GUIDE 8-2 구현 노트)

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


def check_against_data(panel, methods=(BASE_TICK, LIMIT_TICK, DOUBLE)) -> dict:
    """실제 시세로 두 방식을 검증한다 (값 없이 건수만).

    - over/under: 고가 > 상한가 또는 저가 < 하한가인 행 (규칙이 틀렸거나 예외 종목)
    - close_off_tick: 종가가 그날 호가단위 표의 배수가 아닌 행 (표가 틀렸으면 많이 나온다)
    - limit_close: 종가가 기준가 대비 (제한폭 − 1%p) 이상 오르거나 내린 행 중 종가가 계산한 상·하한가와 정확히 같은 비율
    신규상장 첫날(전 거래일 종가 없음)과 거래가 없는 행은 뺀다."""
    import pandas as pd
    df = panel[(panel["volume"] > 0) & panel["factor"].notna() & (panel["base"] > 0)].copy()
    # 호가단위 표 검증: 거래가 있었던 날의 종가는 그날 표의 호가단위 배수여야 한다
    off = [int(c) % tick_size(c, d.date(), mk or "KOSPI") != 0 for c, d, mk in zip(df["close"], df["date"], df["market"])]
    out = {"rows": int(len(df)), "close_off_tick": int(sum(off))}
    for m in methods:
        lims = [limit_prices(b, d.date(), mk or "KOSPI", m) for b, d, mk in zip(df["base"], df["date"], df["market"])]
        lo = pd.Series([x[0] for x in lims], index=df.index)
        up = pd.Series([x[1] for x in lims], index=df.index)
        rate = df["date"].dt.date.map(limit_rate)
        near_up = df["close"] / df["base"] - 1 >= rate - 0.01
        near_dn = df["close"] / df["base"] - 1 <= -(rate - 0.01)
        out[m] = {"over": int((df["high"] > up).sum()), "under": int((df["low"] < lo).sum()),
                  "near_limit_close": int(near_up.sum() + near_dn.sum()),
                  "exact_limit_close": int((near_up & (df["close"] == up)).sum() + (near_dn & (df["close"] == lo)).sum())}
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
