"""거래소 캘린더 기반 시간 정렬 (가이드 4-3 ~ 4-7).

- 시각은 IANA 시간대(Asia/Seoul, America/New_York)와 거래소 캘린더(exchange_calendars)로 계산한다. 고정 오프셋을 쓰지 않는다.
- DART 공시는 정확한 공개 시각이 없으면 접수일 다음 KRX 거래 세션 08:30 KST부터 쓴다 (달력상 다음 날이 아님).
- 회사·시리즈(entity_id + series_id)마다 따로 as-of 정렬하고, 결과는 항상 available_at <= decision_time을 만족해야 한다.
- 한국 장이 쉬는 동안 열린 미국 세션의 수익률은 누적해서 넘긴다 (마지막 종가 복사가 아님). 빠진 세션이 있으면 계산하지 않는다.
"""
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from typing import Optional, Sequence, Union

import exchange_calendars as xcals
import numpy as np
import pandas as pd

KR_VENUE, US_VENUE = "XKRX", "XNYS"
KST = "Asia/Seoul"
KR_DECISION_TIME = time(8, 30)          # 한국 장 시작 전 의사결정 컷오프 (KST)

DayLike = Union[date, datetime, pd.Timestamp, str]


# 라이브러리(exchange_calendars 4.13.2, 최신)에 아직 없는 KRX 휴장일. 날짜마다 근거를 적는다.
# 실제 시세 공급자(금융위원회 주식시세정보)가 그날 전 종목 자료를 주지 않는 것으로 확인했다 (2026-11-11 과거 채우기).
# 라이브러리가 이 날짜들을 넣으면 여기서 지운다 — test_calendar_align이 라이브러리와 겹치면 알려 준다.
KRX_EXTRA_HOLIDAYS = {
    date(2026, 6, 3): "제9회 전국동시지방선거일 (선거일은 KRX 휴장)",
    date(2026, 7, 17): "제헌절 — 시세 자료 없음으로 휴장 확인 (공휴일 지정 근거는 KRX 공지로 확인 필요)",
}


def _xkrx_with_extra_holidays():
    base = type(xcals.get_calendar(KR_VENUE))

    class XKRXCorrected(base):
        @classmethod
        def precomputed_holidays(cls):
            return sorted(set(base.precomputed_holidays()) | {pd.Timestamp(d) for d in KRX_EXTRA_HOLIDAYS})

    return XKRXCorrected()


@lru_cache(maxsize=None)
def calendar(venue: str):
    """거래소 캘린더 (라이브러리 버전은 requirements.txt에 고정, 공식 일정과 정기 대조).
    KRX는 라이브러리에 빠진 휴장일(KRX_EXTRA_HOLIDAYS)을 더한다."""
    if venue == KR_VENUE:
        return _xkrx_with_extra_holidays()
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


def align_asof(decision_grid: pd.DataFrame, observations: pd.DataFrame,
               by: Union[str, Sequence[str]] = ("entity_id", "series_id")) -> pd.DataFrame:
    """그룹별 as-of 정렬 (가이드 4-4). 기본 그룹은 entity_id + series_id.

    series_id만으로 묶으면 같은 지표 이름(예: revenue)을 쓰는 여러 회사의 값이 섞인다. 회사가 없는 거시 시리즈도
    entity_id(예: 국가·발표 기관)를 채워 같은 규칙을 쓴다.
    decision_grid: [decision_time_utc, <by…>] — 그룹마다 의사결정 시각 행이 있어야 한다.
    observations: [available_at_utc, <by…>, ...] — 같은 그룹의 같은 가용 시각은 한 행만 (revision은 PIT 조회로 먼저 정리).
    반환: decision_grid 행마다 그 시각까지 이용 가능했던 마지막 관측값. 못 찾은 행은 지우지 않고 match_status='NO_DATA'.
    """
    keys = [by] if isinstance(by, str) else list(by)
    for df, col in ((decision_grid, "decision_time_utc"), (observations, "available_at_utc")):
        if col not in df or any(k not in df for k in keys):
            raise ValueError(f"{col}, {', '.join(keys)} 열이 필요함")
        if not isinstance(df[col].dtype, pd.DatetimeTZDtype):
            raise ValueError(f"{col}는 시간대가 있는 시각이어야 함")
    if observations.duplicated(keys + ["available_at_utc"]).any():
        raise ValueError("같은 그룹·같은 가용 시각의 관측이 여럿 — revision을 먼저 정리할 것")
    grid = decision_grid.assign(decision_time_utc=decision_grid["decision_time_utc"].dt.tz_convert("UTC"),
                                _row=np.arange(len(decision_grid)))
    obs = observations.assign(available_at_utc=observations["available_at_utc"].dt.tz_convert("UTC"))
    out = pd.merge_asof(grid.sort_values("decision_time_utc"), obs.sort_values("available_at_utc"),
                        left_on="decision_time_utc", right_on="available_at_utc", by=keys,
                        direction="backward", allow_exact_matches=True)
    out = out.sort_values("_row").drop(columns="_row").reset_index(drop=True)
    matched = out["available_at_utc"].notna()
    # 필수 불변조건: 쓰인 관측은 모두 의사결정 시각 이전(같은 시각 포함)에 이용 가능했어야 한다
    assert (out.loc[matched, "available_at_utc"] <= out.loc[matched, "decision_time_utc"]).all(), "PIT 시간 역전"
    out["match_status"] = np.where(matched, "OK", "NO_DATA")
    return out


def us_cumulative_return(us_close: pd.Series, prev_cutoff_utc, cutoff_utc,
                         provider_delay: timedelta = timedelta(minutes=30)) -> dict:
    """한국의 직전 컷오프 이후 이번 컷오프까지 이용 가능해진 미국 세션들의 누적 수익률 (가이드 4-5).

    us_close: 종가 Series, index = 그 종가가 이용 가능해진 시각(UTC) = us_close_available_at(세션, provider_delay).
    같은 통화(USD) 기준. 거래소 캘린더로 '있어야 할' 세션을 구해 받은 종가와 대조한다.

    반환 status:
      OK              — return = P_end / P_start − 1 = Π(1 + r_j) − 1
      NO_US_SESSION   — 그 사이 미국 휴장이라 새 세션이 없음: return 0.0
      INCOMPLETE      — 시작 기준 종가나 구간 안 종가가 빠짐: return NaN (결측이 큰 움직임을 숨길 수 있어 남은 값으로
                        계산하지 않고, 더 오래된 종가로 대신하지도 않는다). missing_sessions에 빠진 세션 날짜
    """
    if not isinstance(us_close.index, pd.DatetimeIndex) or us_close.index.tz is None:
        raise ValueError("us_close의 index는 시간대 있는 가용 시각이어야 함")
    s = us_close.sort_index()
    s.index = s.index.tz_convert("UTC")
    prev, cut = _utc(prev_cutoff_utc), _utc(cutoff_utc)
    if prev >= cut:
        raise ValueError("prev_cutoff_utc < cutoff_utc 이어야 함")
    cal = calendar(US_VENUE)
    lo = prev.tz_convert(None).normalize() - pd.Timedelta(days=14)
    hi = cut.tz_convert(None).normalize() + pd.Timedelta(days=1)
    avail = {d: us_close_available_at(d, provider_delay) for d in cal.sessions_in_range(lo, hi)}
    window = [d for d, t in avail.items() if prev < t <= cut]
    start = max((d for d, t in avail.items() if t <= prev), default=None)
    observed = set(s.index)
    missing = [d for d in ([start] if start is not None else []) + window if avail[d] not in observed]
    out = {"return": np.nan, "n_sessions": len(window), "expected_sessions": [str(d.date()) for d in window],
           "missing_sessions": [str(d.date()) for d in missing], "start_at": None, "end_at": None}
    if not window:
        return {**out, "return": 0.0, "status": "NO_US_SESSION",
                "start_at": avail[start] if start is not None and start not in missing else None}
    if missing or start is None:
        return {**out, "status": "INCOMPLETE"}
    p0, p1 = s[avail[start]], s[avail[window[-1]]]
    return {**out, "return": float(p1 / p0 - 1), "status": "OK", "start_at": avail[start], "end_at": avail[window[-1]]}
