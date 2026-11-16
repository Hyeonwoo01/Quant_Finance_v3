"""거래 비용 모델 v1 (가이드 8-3, 8-4, 8-5). 체결 가정이며 실제 체결 성능이 아니다.

- **매도 세금 (증권거래세 + 코스피 농어촌특별세, 매도 금액 기준):** 날짜별 합계 세율.
  코스피(거래세 + 농특세 0.15%)와 코스닥(거래세)의 합계가 같도록 정해져 왔으므로 시장 구분 없이 한 표를 쓴다.
    ~2019-06-02 0.30% · 2019-06-03 0.25% · 2021-01-01 0.23% · 2023-01-01 0.20% · 2024-01-01 0.18%
    · 2025-01-01 0.15% · 2026-01-01 0.20% (2025 세제개편: 코스피 거래세 0.05% + 농특세 0.15%, 코스닥 0.20%)
  2021년 0.23%, 2025년 0.15%, 2026년 0.20%는 보도로 확인했다. 2019년·2023년·2024년 값은 2차 자료와 기억에
  기댄 것이라 국세청·기재부 원문으로 확인해야 한다 (확인 전까지 시나리오 민감도로 본다).
- **수수료:** 매수·매도 각각 commission (기본 0.015%, 온라인 증권사 수준 가정).
- **슬리피지:** 시가 단일가로 사고팔므로 호가 스프레드는 없다고 보지 않고, 호가단위 몇 틱만큼의 불리한 가격을
  가정한다 (가격대별 호가단위 / 가격). 저가주일수록 커진다. 여기에 extra_bps를 더한다.
- **용량:** 종목당 주문 금액이 21세션 평균 거래대금의 max_participation을 넘으면 넘는 만큼은 체결되지 않은 것으로
  보고 현금으로 남긴다 (시가 단일가 거래대금은 하루 거래대금보다 훨씬 작다는 점을 반영한 보수적 가정).
시나리오 세 개를 함께 보고한다 (8-5: 한 가지 낙관적 가정만으로 보고하지 않는다).
"""
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from evaluation.kr_market_rules import _tick_arr

SELL_TAX = ((date(1900, 1, 1), 0.0030), (date(2019, 6, 3), 0.0025), (date(2021, 1, 1), 0.0023),
            (date(2023, 1, 1), 0.0020), (date(2024, 1, 1), 0.0018), (date(2025, 1, 1), 0.0015),
            (date(2026, 1, 1), 0.0020))


@dataclass(frozen=True)
class CostScenario:
    name: str
    commission: float = 0.00015
    slippage_ticks: float = 1.0
    extra_bps: float = 0.0
    max_participation: float = 0.05
    tax_shift: float = 0.0              # 세율 불확실성 민감도 (예: +0.0005)


SCENARIOS = (
    CostScenario("base", slippage_ticks=1.0, extra_bps=0.0, max_participation=0.05),
    CostScenario("conservative", slippage_ticks=2.0, extra_bps=5.0, max_participation=0.02, tax_shift=0.0005),
    CostScenario("stress", slippage_ticks=3.0, extra_bps=15.0, max_participation=0.01, tax_shift=0.0005),
)
VERSION = "costs_v1"


def sell_tax(days) -> np.ndarray:
    d = pd.to_datetime(pd.Series(days)).to_numpy().astype("datetime64[D]")
    starts = np.array([np.datetime64(s) for s, _ in SELL_TAX])
    rates = np.array([r for _, r in SELL_TAX])
    return rates[np.searchsorted(starts, d, side="right") - 1]


def slippage(price, days, markets, sc: CostScenario) -> np.ndarray:
    """한쪽 거래의 슬리피지 비율 (호가단위 × 틱 수 / 가격 + extra_bps)."""
    price = np.asarray(price, dtype=float)
    d = pd.to_datetime(pd.Series(days)).to_numpy().astype("datetime64[D]")
    kosdaq = np.asarray(pd.Series(markets).astype(str).str.upper() == "KOSDAQ")
    safe = np.where(price > 0, price, np.nan)
    return sc.slippage_ticks * _tick_arr(np.nan_to_num(safe, nan=1.0), d, kosdaq) / safe + sc.extra_bps / 1e4
