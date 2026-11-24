"""실전 예측 기록: 그달 첫 거래일만, D 09:00 전에만, D−2 종가가 없으면 기록 안 함, 정산과 요약. 합성 데이터."""
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from evaluation.forecast_audit import ForecastLedger
from orchestration import live_forecast as lf
from storage.visibility import PRIVATE_MARKER
from tests.unit.test_walkforward import synth_panel

UTC = timezone.utc
PANEL = synth_panel(n_codes=60)


def upto(day):
    return lambda d, s, e, **kw: PANEL[PANEL["date"] <= pd.Timestamp(day)]


def test_next_decision_is_first_session_of_month():
    assert lf.next_decision(datetime(2020, 10, 30, 12, tzinfo=UTC)) == (date(2020, 11, 2), True)   # 금요일 밤 KST
    assert lf.next_decision(datetime(2020, 11, 2, 1, tzinfo=UTC)) == (date(2020, 11, 3), False)    # 장 시작 뒤
    assert lf.two_sessions_before(date(2020, 11, 2)) == date(2020, 10, 29)


def test_forecast_records_once_before_open_and_settles(tmp_path):
    (tmp_path / PRIVATE_MARKER).write_text("private\n")
    now = datetime(2020, 10, 30, 12, tzinfo=UTC)
    r = lf.forecast(tmp_path, now, loader=upto("2020-10-30"), log=lambda s: None, clock=lambda: now)
    assert r["status"] == "recorded" and r["records"] == 2 * r["universe"]
    f = ForecastLedger(tmp_path).forecasts()
    assert {x.model_id for x in f} == {"H1_ens_price_v1", "H2_mlp_price_v1"}
    assert all(x.data_cutoff_utc <= x.decision_time_utc and x.score is not None for x in f)
    assert lf.forecast(tmp_path, now, loader=upto("2020-10-30"), log=lambda s: None, clock=lambda: now)["status"] == "already"
    # 장 시작(11-02 09:00 KST = 00:00 UTC) 뒤에 만든 기록은 장부가 거부 (다른 디렉터리에서)
    late = tmp_path / "late"
    late.mkdir()
    (late / PRIVATE_MARKER).write_text("private\n")
    r_late = lf.forecast(late, now, loader=upto("2020-10-30"), log=lambda s: None,
                         clock=lambda: datetime(2020, 11, 2, 0, 30, tzinfo=UTC))
    assert r_late["status"] == "late" and ForecastLedger(late).forecasts() == []
    ticks = iter([now, datetime(2020, 11, 2, 0, 1, tzinfo=UTC)])                    # 학습 중에 장이 열린 경우
    assert lf.forecast(late, now, loader=upto("2020-10-30"), log=lambda s: None, clock=lambda: next(ticks))["status"] == "late"
    with pytest.raises(ValueError, match="소급 기록 금지"):                         # 장부 자체도 거부 (이중 장치)
        from evaluation.forecast_audit import ForecastRecord
        ForecastLedger(late).record_many([ForecastRecord(
            forecast_id="x", created_at_utc=datetime(2020, 11, 2, 0, 30, tzinfo=UTC),
            decision_time_utc=datetime(2020, 11, 1, 23, 30, tzinfo=UTC),
            data_cutoff_utc=datetime(2020, 10, 30, 4, tzinfo=UTC), ticker="000001", model_id="m",
            target_definition_id="RET_21D", holding_period_sessions=21)])
    s = lf.settle(tmp_path, datetime(2020, 12, 20, tzinfo=UTC), loader=upto("2020-12-30"), log=lambda s: None)
    assert s["settled_dates"] == 1
    summ = lf.summary(ForecastLedger(tmp_path))
    assert set(summ["model"]) == {"H1_ens_price_v1", "H2_mlp_price_v1"} and summ["ic"].notna().all()


def test_stale_data_or_not_due_records_nothing(tmp_path):
    (tmp_path / PRIVATE_MARKER).write_text("private\n")
    now = datetime(2020, 10, 30, 12, tzinfo=UTC)
    r = lf.forecast(tmp_path, now, loader=upto("2020-10-28"), log=lambda s: None, clock=lambda: now)   # 10-29 종가 없음
    assert r["status"] == "stale" and ForecastLedger(tmp_path).forecasts() == []
    r2 = lf.forecast(tmp_path, datetime(2020, 10, 20, 12, tzinfo=UTC), loader=upto("2020-10-20"), log=lambda s: None)
    assert r2["status"] == "not_due"


def test_dry_run_computes_without_recording(tmp_path):
    (tmp_path / PRIVATE_MARKER).write_text("private\n")
    r = lf.dry_run(tmp_path, date(2020, 11, 2), loader=upto("2020-12-30"), log=lambda s: None)
    assert r["scores_ok"] and r["fresh"] == 1.0 and r["universe"] > 0
    assert not (tmp_path / "forecast_audit").exists()
