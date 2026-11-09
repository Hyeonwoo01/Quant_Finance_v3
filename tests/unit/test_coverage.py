"""엔진별 데이터 커버리지 상태 (가이드 3-6): 하나의 최소 이력을 모든 엔진에 강제하지 않는다."""
from datetime import datetime, timedelta, timezone

import pytest

from core.coverage import (DEFAULT_REQUIREMENTS, CoverageStatus, EngineRequirement, InputRequirement, InputSnapshot,
                           assess)

AS_OF = datetime(2026, 11, 9, 0, 0, tzinfo=timezone.utc)
FRESH = AS_OF - timedelta(days=1)


def snap(name, n, last=FRESH, stale=False):
    return {name: InputSnapshot(name, n, last, stale)}


def test_new_listing_blocks_garch_but_not_financial_health():
    """신규 상장: 주가 이력이 짧아 GARCH는 산출 불가지만, 재무제표 기반 기업 분석까지 막지 않는다."""
    g = assess(DEFAULT_REQUIREMENTS["garch"], snap("daily_returns", 60), AS_OF)
    h = assess(DEFAULT_REQUIREMENTS["financial_health"], snap("quarterly_financials", 5, last=None), AS_OF)
    assert g.status is CoverageStatus.INSUFFICIENT and not g.can_compute and g.missing_inputs == ("daily_returns",)
    assert h.status is CoverageStatus.LIMITED and h.can_compute                  # 4분기 이상, 권장 8분기 미만


def test_status_levels_for_one_required_input():
    req = EngineRequirement("e", "t", (InputRequirement("x", min_obs=10, preferred_obs=20, max_age=timedelta(days=7)),))
    assert assess(req, snap("x", 25), AS_OF).status is CoverageStatus.SUFFICIENT
    assert assess(req, snap("x", 15), AS_OF).status is CoverageStatus.LIMITED
    assert assess(req, snap("x", 5), AS_OF).status is CoverageStatus.INSUFFICIENT
    assert assess(req, {}, AS_OF).status is CoverageStatus.INSUFFICIENT
    old = assess(req, snap("x", 25, last=AS_OF - timedelta(days=30)), AS_OF)
    assert old.status is CoverageStatus.STALE and not old.can_compute
    assert assess(req, snap("x", 25, stale=True), AS_OF).status is CoverageStatus.STALE   # upstream 세션 기준 판정
    assert assess(req, snap("x", 25, last=None), AS_OF).status is CoverageStatus.STALE    # 최신성을 확인할 수 없음


def test_worst_input_wins_and_optional_inputs_only_limit():
    req = DEFAULT_REQUIREMENTS["global_exposure"]
    ok = {**snap("segment_disclosure", 2), **snap("industry_drivers", 3)}
    assert assess(req, ok, AS_OF).status is CoverageStatus.SUFFICIENT
    no_driver = assess(req, snap("segment_disclosure", 2), AS_OF)                  # 선택 입력 없음 → 제한
    assert no_driver.status is CoverageStatus.LIMITED and "industry_drivers" in no_driver.missing_inputs
    stale_driver = assess(req, {**snap("segment_disclosure", 2), **snap("industry_drivers", 3, stale=True)}, AS_OF)
    assert stale_driver.status is CoverageStatus.LIMITED
    beta = DEFAULT_REQUIREMENTS["rolling_beta"]
    mixed = assess(beta, {**snap("stock_returns", 300), **snap("market_returns", 50)}, AS_OF)
    assert mixed.status is CoverageStatus.INSUFFICIENT and mixed.missing_inputs == ("market_returns",)
    assert any("market_returns" in r for r in mixed.reasons)


def test_future_data_and_naive_times_are_rejected():
    req = DEFAULT_REQUIREMENTS["garch"]
    with pytest.raises(ValueError, match="시간 역전"):
        assess(req, snap("daily_returns", 800, last=AS_OF + timedelta(hours=1)), AS_OF)
    with pytest.raises(ValueError, match="시간대"):
        assess(req, snap("daily_returns", 800), datetime(2026, 11, 9))


def test_requirement_validation_and_versions():
    with pytest.raises(ValueError):
        InputRequirement("x", min_obs=10, preferred_obs=5)
    assert all(r.version and r.inputs for r in DEFAULT_REQUIREMENTS.values())
    assert assess(DEFAULT_REQUIREMENTS["garch"], snap("daily_returns", 800), AS_OF).requirement_version == "0.1"
