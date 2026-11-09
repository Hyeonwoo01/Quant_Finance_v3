"""거래소 캘린더 기반 시간 정렬 (가이드 4-3 ~ 4-7).

- 시각은 IANA 시간대(Asia/Seoul, America/New_York)와 거래소 캘린더(exchange_calendars)로 계산한다. 고정 오프셋을 쓰지 않는다.
- DART 공시는 정확한 공개 시각이 없으면 접수일 다음 KRX 거래 세션 08:30 KST부터 쓴다 (달력상 다음 날이 아님).
- 시리즈마다 따로 as-of 정렬하고, 결과는 항상 available_at <= decision_time을 만족해야 한다.
- 한국 장이 쉬는 동안 열린 미국 세션의 수익률은 누적해서 넘긴다 (마지막 종가 복사가 아님).
"""
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from typing import Optional, Union

import exchange_calendars as xcals
import numpy as np
import pandas as pd

KR_VENUE, US_VENUE = "XKRX", "XNYS"
KST = "Asia/Seoul"
KR_DECISION_TIME = time(8, 30)          # 한국 장 시작 전 의사결정 컷오프 (KST)

DayLike = Union[date, datetime, pd.Timestamp, str]


@lru_cache(maxsize=None)
def calendar(venue: str):
    """거래소 캘린더 (라이브러리 버전은 requirements.txt에 고정, 공식 일정과 정기 대조)."""
    return xcals.get_calendar(venue)


def _day(d: DayLike) -> pd.Timestamp:
    """달력 날짜(시간대 없는 자정)로. 시간대 있는 시각이 오면 그 시각의 현지 날짜가 아니라 오류로 거부한다."""
    t = pd.Timestamp(d)
    if t.tzinfo is not None:
        raise ValueError("달력 날짜 자리에 시간대 있는 시각이 들어옴 — 거래소 현지 날짜로 바꿔서 넘긴다")
    return t.normalize()


def _utc(t) -> pd.Timestamp:
    t = pd.Timestamp(t)
    if t.tzinfo is None:
        raise ValueError("시간대 없는 시각은 받지 않는다")
    return t.tz_convert("UTC")


def next_session_after(day: DayLike, venue: str = KR_VENUE) -> pd.Timestamp:
    """그 달력 날짜 '다음'의 첫 거래 세션 (그날이 세션이어도 다음 세션). 주말·공휴일·연휴를 건너뛴다."""
    return calendar(venue).date_to_session(_day(day) + pd.Timedelta(days=1), direction="next")


def kr_decision_cutoff(session: DayLike) -> pd.Timestamp:
    """그 KRX 세션 날짜의 08:30 KST를 UTC로."""
    s = _day(session)
    if not calendar(KR_VENUE).is_session(s):
        raise ValueError(f"{s.date()}는 KRX 거래 세션이 아님")
    return pd.Timestamp(datetime.combine(s.date(), KR_DECISION_TIME)).tz_localize(KST).tz_convert("UTC")


def first_kr_cutoff_at_or_after(t_utc) -> pd.Timestamp:
    """그 시각 이후(같은 시각 포함) 처음 오는 KRX 세션 08:30 KST 컷오프 (UTC)."""
    t = _utc(t_utc)
    local_day = t.tz_convert(KST).tz_localize(None).normalize()
    cal = calendar(KR_VENUE)
    s = cal.date_to_session(local_day, direction="next")
    cut = kr_decision_cutoff(s)
    return cut if cut >= t else kr_decision_cutoff(next_session_after(s))


def dart_available_at(receipt_date: DayLike, exact_publication_at=None, provider_available_at=None) -> pd.Timestamp:
    """DART 공시의 보수적 가용 시각 (UTC, 가이드 4-3).

    rcept_dt(접수일, YYYYMMDD)는 시각이 아니므로, 정확한 공개 시각이 없으면 접수일 다음 KRX 세션의 08:30 KST.
    정확한 공개 시각이 있으면 공개 시각과 공급자 가용 시각 중 늦은 것 이후의 첫 08:30 KST 컷오프.
    """
    if exact_publication_at is None:
        return kr_decision_cutoff(next_session_after(receipt_date, KR_VENUE))
    eff = _utc(exact_publication_at)
    if provider_available_at is not None:
        eff = max(eff, _utc(provider_available_at))
    return first_kr_cutoff_at_or_after(eff)


def session_close_utc(session: DayLike, venue: str = US_VENUE) -> pd.Timestamp:
    """그 세션의 실제 마감 시각 (UTC). 조기폐장·서머타임은 캘린더가 반영한다."""
    s = _day(session)
    cal = calendar(venue)
    if not cal.is_session(s):
        raise ValueError(f"{s.date()}는 {venue} 거래 세션이 아님")
    return _utc(cal.session_close(s))


def us_close_available_at(session: DayLike, provider_delay: timedelta = timedelta(minutes=30)) -> pd.Timestamp:
    """미국 종가가 우리에게 이용 가능해지는 시각 = 실제 마감 + 공급자 지연. 마감 즉시 확정값이 온다고 가정하지 않는다."""
    return session_close_utc(session, US_VENUE) + pd.Timedelta(provider_delay)


def kr_cutoffs(start: DayLike, end: DayLike) -> pd.DatetimeIndex:
    """기간 안의 모든 KRX 세션 08:30 KST 컷오프 (UTC)."""
    sessions = calendar(KR_VENUE).sessions_in_range(_day(start), _day(end))
    return pd.DatetimeIndex([kr_decision_cutoff(s) for s in sessions])


def missing_sessions(last_session: DayLike, as_of_session: DayLike, venue: str = KR_VENUE) -> int:
    """마지막 관측 세션 뒤로 as_of 세션까지 빠진 거래 세션 수 (일별 데이터의 staleness, 가이드 4-7).
    달력 일수가 아니라 거래 세션으로 세므로 연휴는 누락으로 치지 않는다."""
    last, now = _day(last_session), _day(as_of_session)
    if last > now:
        raise ValueError("마지막 관측 세션이 as_of 세션보다 늦음 — 시간 역전")
    return max(len(calendar(venue).sessions_in_range(last, now)) - 1, 0)


def align_asof(decision_grid: pd.DataFrame, observations: pd.DataFrame, by: str = "series_id") -> pd.DataFrame:
    """시리즈별 as-of 정렬 (가이드 4-4).

    decision_grid: [decision_time_utc, <by>] — 시리즈마다 의사결정 시각 행이 있어야 한다.
    observations: [available_at_utc, <by>, ...] — 같은 시리즈의 같은 가용 시각은 한 행만 (revision은 PIT 조회로 먼저 정리).
    반환: decision_grid 행마다 그 시각까지 이용 가능했던 마지막 관측값. 못 찾은 행은 지우지 않고 match_status='NO_DATA'.
    """
    for df, col in ((decision_grid, "decision_time_utc"), (observations, "available_at_utc")):
        if col not in df or by not in df:
            raise ValueError(f"{col}, {by} 열이 필요함")
        if not isinstance(df[col].dtype, pd.DatetimeTZDtype):
            raise ValueError(f"{col}는 시간대가 있는 시각이어야 함")
    if observations.duplicated([by, "available_at_utc"]).any():
        raise ValueError("같은 시리즈·같은 가용 시각의 관측이 여럿 — revision을 먼저 정리할 것")
    grid = decision_grid.assign(decision_time_utc=decision_grid["decision_time_utc"].dt.tz_convert("UTC"),
                                _row=np.arange(len(decision_grid)))
    obs = observations.assign(available_at_utc=observations["available_at_utc"].dt.tz_convert("UTC"))
    out = pd.merge_asof(grid.sort_values("decision_time_utc"), obs.sort_values("available_at_utc"),
                        left_on="decision_time_utc", right_on="available_at_utc", by=by,
                        direction="backward", allow_exact_matches=True)
    out = out.sort_values("_row").drop(columns="_row").reset_index(drop=True)
    matched = out["available_at_utc"].notna()
    # 필수 불변조건: 쓰인 관측은 모두 의사결정 시각 이전(같은 시각 포함)에 이용 가능했어야 한다
    assert (out.loc[matched, "available_at_utc"] <= out.loc[matched, "decision_time_utc"]).all(), "PIT 시간 역전"
    out["match_status"] = np.where(matched, "OK", "NO_DATA")
    return out


def us_cumulative_return(us_close: pd.Series, prev_cutoff_utc, cutoff_utc) -> dict:
    """한국의 직전 컷오프 이후 이번 컷오프까지 이용 가능해진 미국 세션들의 누적 수익률 (가이드 4-5).

    us_close: 종가 Series, index = 그 종가가 이용 가능해진 시각(UTC, us_close_available_at). 같은 통화(USD) 기준.
    반환: return(= P_end / P_start − 1 = Π(1 + r_j) − 1), n_sessions, start_at, end_at. 직전 컷오프까지의 종가가 없으면 return NaN.
    한국 연휴 중 열린 미국 세션은 모두 들어가고, 미국 휴장이라 새 세션이 없으면 0과 n_sessions=0.
    """
    if not isinstance(us_close.index, pd.DatetimeIndex) or us_close.index.tz is None:
        raise ValueError("us_close의 index는 시간대 있는 가용 시각이어야 함")
    s = us_close.sort_index()
    s.index = s.index.tz_convert("UTC")
    prev, cut = _utc(prev_cutoff_utc), _utc(cutoff_utc)
    if prev >= cut:
        raise ValueError("prev_cutoff_utc < cutoff_utc 이어야 함")
    before = s[s.index <= prev]
    window = s[(s.index > prev) & (s.index <= cut)]
    if before.empty:
        return {"return": np.nan, "n_sessions": len(window), "start_at": None,
                "end_at": window.index[-1] if len(window) else None}
    p0 = before.iloc[-1]
    p1 = window.iloc[-1] if len(window) else p0
    return {"return": float(p1 / p0 - 1), "n_sessions": int(len(window)), "start_at": before.index[-1],
            "end_at": window.index[-1] if len(window) else None}
