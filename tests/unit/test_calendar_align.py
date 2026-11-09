"""거래소 캘린더 기반 시간 정렬 (가이드 4-3 ~ 4-7). 날짜는 exchange_calendars XKRX·XNYS 2026 일정 기준."""
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from core.calendar_align import (align_asof, dart_available_at, first_kr_cutoff_at_or_after, kr_cutoffs,
                                 kr_decision_cutoff, missing_sessions, next_session_after, session_close_utc,
                                 us_close_available_at, us_cumulative_return)


def utc(*a):
    return pd.Timestamp(datetime(*a), tz="UTC")


def kst_0830(d):
    return pd.Timestamp(f"{d} 08:30", tz="Asia/Seoul").tz_convert("UTC")


# ---------- KRX 세션과 DART 가용 시각 ----------

def test_next_session_skips_weekend_and_chuseok():
    assert next_session_after("2026-11-06").date() == date(2026, 11, 9)       # 금 → 월
    assert next_session_after("2026-11-09").date() == date(2026, 11, 10)      # 세션이어도 '다음' 세션
    assert next_session_after("2026-09-23").date() == date(2026, 9, 28)       # 추석 연휴(9/24~25)와 주말
    assert next_session_after("2026-10-02").date() == date(2026, 10, 6)       # 개천절 대체휴일(10/5)


def test_dart_receipt_date_uses_next_krx_session_0830():
    assert dart_available_at("20261106") == kst_0830("2026-11-09")
    assert dart_available_at(date(2026, 11, 7)) == kst_0830("2026-11-09")     # 토요일 접수
    assert dart_available_at("2026-09-23") == kst_0830("2026-09-28")
    assert dart_available_at("2026-11-09") == utc(2026, 11, 9, 23, 30)        # 08:30 KST = 전날 23:30 UTC


def test_dart_with_exact_publication_time():
    # 장 마감 후 공개 → 다음 세션 08:30
    assert dart_available_at("2026-11-06", pd.Timestamp("2026-11-06 16:00", tz="Asia/Seoul")) == kst_0830("2026-11-09")
    # 장 시작 전 공개 → 그날 08:30
    assert dart_available_at("2026-11-09", pd.Timestamp("2026-11-09 07:00", tz="Asia/Seoul")) == kst_0830("2026-11-09")
    # 정확히 08:30 공개는 그날 컷오프에 쓴다 (available_at <= decision_time)
    assert dart_available_at("2026-11-09", kst_0830("2026-11-09")) == kst_0830("2026-11-09")
    # 공급자 가용 시각이 더 늦으면 그쪽을 따른다
    pub, prov = pd.Timestamp("2026-11-09 07:00", tz="Asia/Seoul"), pd.Timestamp("2026-11-09 09:00", tz="Asia/Seoul")
    assert dart_available_at("2026-11-09", pub, prov) == kst_0830("2026-11-10")


def test_naive_and_aware_inputs_are_not_guessed():
    with pytest.raises(ValueError, match="시간대 없는"):
        dart_available_at("2026-11-09", datetime(2026, 11, 9, 7, 0))
    with pytest.raises(ValueError, match="달력 날짜"):
        next_session_after(pd.Timestamp("2026-11-09", tz="UTC"))


def test_kr_cutoff_requires_session_and_first_cutoff_rolls_over_holidays():
    with pytest.raises(ValueError, match="KRX 거래 세션이 아님"):
        kr_decision_cutoff("2026-09-24")
    assert first_kr_cutoff_at_or_after(utc(2026, 9, 23, 0, 0)) == kst_0830("2026-09-28")   # 9/23 09:00 KST 이후
    assert first_kr_cutoff_at_or_after(utc(2026, 9, 25, 3, 0)) == kst_0830("2026-09-28")   # 연휴 중
    assert first_kr_cutoff_at_or_after(utc(2026, 11, 8, 23, 30)) == kst_0830("2026-11-09")  # 정확히 컷오프


def test_kr_cutoffs_cover_only_sessions():
    cuts = kr_cutoffs("2026-09-21", "2026-09-30")
    assert [c.tz_convert("Asia/Seoul").date() for c in cuts] == [
        date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30)]


# ---------- 미국 마감 시각 ----------

def test_us_close_follows_dst_and_early_close():
    assert session_close_utc("2026-03-06") == utc(2026, 3, 6, 21, 0)   # 서머타임 전
    assert session_close_utc("2026-03-09") == utc(2026, 3, 9, 20, 0)   # 3/8 서머타임 시작 후
    assert session_close_utc("2026-10-30") == utc(2026, 10, 30, 20, 0)
    assert session_close_utc("2026-11-02") == utc(2026, 11, 2, 21, 0)  # 11/1 서머타임 종료 후
    assert session_close_utc("2026-11-27") == utc(2026, 11, 27, 18, 0)  # 추수감사절 다음 날 조기폐장
    with pytest.raises(ValueError, match="거래 세션이 아님"):
        session_close_utc("2026-11-26")


def test_us_close_available_includes_provider_delay():
    assert us_close_available_at("2026-11-02") == utc(2026, 11, 2, 21, 30)
    assert us_close_available_at("2026-11-02", timedelta(hours=2)) == utc(2026, 11, 2, 23, 0)


# ---------- 시리즈별 as-of 정렬 ----------

def _grid(times, series):
    return pd.DataFrame({"decision_time_utc": pd.to_datetime(times, utc=True, format="ISO8601"), "series_id": series})


def _obs(times, series, values):
    return pd.DataFrame({"available_at_utc": pd.to_datetime(times, utc=True, format="ISO8601"), "series_id": series, "value": values})


def test_align_asof_is_per_series_and_never_uses_future():
    grid = _grid(["2026-11-09 00:00", "2026-11-09 00:00", "2026-11-10 00:00", "2026-11-10 00:00"], ["A", "B", "A", "B"])
    obs = _obs(["2026-11-08 12:00", "2026-11-09 00:00", "2026-11-09 06:00", "2026-11-10 00:00:01"],
               ["A", "A", "B", "B"], [1.0, 2.0, 10.0, 11.0])
    out = align_asof(grid, obs)
    assert out["series_id"].tolist() == ["A", "B", "A", "B"]           # 입력 순서 유지
    assert out["value"].tolist()[0] == 2.0                              # 같은 시각은 사용 가능
    assert np.isnan(out["value"].tolist()[1])                           # B는 아직 없음 — 다른 시리즈 값을 쓰지 않는다
    assert out["value"].tolist()[2:] == [2.0, 10.0]                     # 1초 늦은 B 관측은 쓰지 않는다
    assert out["match_status"].tolist() == ["OK", "NO_DATA", "OK", "OK"]
    m = out["match_status"] == "OK"
    assert (out.loc[m, "available_at_utc"] <= out.loc[m, "decision_time_utc"]).all()


def test_align_asof_accepts_other_timezones_and_rejects_bad_input():
    grid = _grid(["2026-11-09 00:00"], ["A"])
    obs = _obs(["2026-11-08 23:30"], ["A"], [1.0])
    obs["available_at_utc"] = obs["available_at_utc"].dt.tz_convert("Asia/Seoul")
    assert align_asof(grid, obs)["value"].tolist() == [1.0]

    with pytest.raises(ValueError, match="시간대가 있는"):
        align_asof(grid.assign(decision_time_utc=grid["decision_time_utc"].dt.tz_localize(None)), obs)
    with pytest.raises(ValueError, match="revision"):
        align_asof(grid, pd.concat([obs, obs]))
    with pytest.raises(ValueError, match="열이 필요"):
        align_asof(grid.drop(columns="series_id"), obs)


# ---------- 한국 연휴 중 미국 수익률 누적 ----------

def _us_close(prices: dict) -> pd.Series:
    idx = pd.DatetimeIndex([us_close_available_at(d) for d in prices])
    return pd.Series(list(prices.values()), index=idx, dtype=float)


def test_us_return_accumulates_over_chuseok():
    close = _us_close({"2026-09-21": 99.0, "2026-09-22": 100.0, "2026-09-23": 102.0, "2026-09-24": 101.0,
                       "2026-09-25": 104.0, "2026-09-28": 110.0})
    prev, cut = kr_decision_cutoff("2026-09-23"), kr_decision_cutoff("2026-09-28")
    assert (prev, cut) == (utc(2026, 9, 22, 23, 30), utc(2026, 9, 27, 23, 30))
    r = us_cumulative_return(close, prev, cut)
    assert r["n_sessions"] == 3                                   # 9/23, 9/24, 9/25 세션 모두
    assert r["return"] == pytest.approx(104.0 / 100.0 - 1)        # 마지막 종가 복사가 아니라 누적
    rets = close.pct_change().loc[(close.index > prev) & (close.index <= cut)]
    assert r["return"] == pytest.approx(float(np.prod(1 + rets) - 1))
    assert r["start_at"] == us_close_available_at("2026-09-22") and r["end_at"] == us_close_available_at("2026-09-25")


def test_us_return_edge_cases():
    close = _us_close({"2026-11-25": 100.0, "2026-11-27": 101.0})
    # 추수감사절(11/26) 미국 휴장: 11/27 08:30 KST 컷오프 사이에는 새 세션 없음
    r = us_cumulative_return(close, kr_decision_cutoff("2026-11-26"), kr_decision_cutoff("2026-11-27"))
    assert r["return"] == 0.0 and r["n_sessions"] == 0 and r["end_at"] is None
    # 직전 컷오프까지의 종가가 없으면 NaN
    r = us_cumulative_return(close, utc(2026, 11, 20), kr_decision_cutoff("2026-11-26"))
    assert np.isnan(r["return"]) and r["start_at"] is None
    with pytest.raises(ValueError, match="prev_cutoff_utc < cutoff_utc"):
        us_cumulative_return(close, utc(2026, 11, 27), utc(2026, 11, 27))
    with pytest.raises(ValueError, match="index"):
        us_cumulative_return(pd.Series([1.0], index=pd.DatetimeIndex(["2026-11-27"])), utc(2026, 11, 26), utc(2026, 11, 27))


# ---------- 세션 기준 staleness ----------

def test_missing_sessions_counts_trading_sessions_not_calendar_days():
    assert missing_sessions("2026-09-23", "2026-09-28") == 1      # 달력 5일, 연휴 제외 세션 1개
    assert missing_sessions("2026-11-06", "2026-11-09") == 1      # 주말
    assert missing_sessions("2026-11-09", "2026-11-09") == 0
    assert missing_sessions("2026-11-02", "2026-11-09") == 5
    with pytest.raises(ValueError, match="시간 역전"):
        missing_sessions("2026-11-10", "2026-11-09")
