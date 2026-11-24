"""OpenDART 하루 호출 상한: 하루에 여러 번 돌아도(예약 시각 여러 개) 합이 상한을 넘지 않는다."""
from datetime import datetime, timezone

from orchestration.opendart_daily import DAILY_CALL_CAP, calls_used_today, daily_budget, record_calls

UTC = timezone.utc


def test_daily_budget_shares_cap_across_runs_by_kst_day(tmp_path):
    t1 = datetime(2026, 11, 24, 14, 0, tzinfo=UTC)          # 11-24 23:00 KST
    assert daily_budget(tmp_path, t1, 8000) == 8000
    record_calls(tmp_path, t1, 8000)
    record_calls(tmp_path, t1, 6000)
    assert calls_used_today(tmp_path, t1) == 14000
    assert daily_budget(tmp_path, t1, 8000) == DAILY_CALL_CAP - 14000
    t2 = datetime(2026, 11, 24, 15, 30, tzinfo=UTC)         # 11-25 00:30 KST → 새 날
    assert calls_used_today(tmp_path, t2) == 0 and daily_budget(tmp_path, t2, 8000) == 8000
    record_calls(tmp_path, datetime(2026, 12, 20, tzinfo=UTC), 1)
    assert calls_used_today(tmp_path, t1) == 0                # 14일 지난 기록은 지움
