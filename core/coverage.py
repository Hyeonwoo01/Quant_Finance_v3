"""데이터 커버리지와 분석 가능 상태 (가이드 3-6).

권한(core/data_contracts)과 충분성은 다른 문제다. 분석 엔진마다 필요한 입력을 정의하고, as_of 시점에 이용 가능했던
데이터로 SUFFICIENT / LIMITED / INSUFFICIENT / STALE을 판정한다. 모든 종목에 하나의 최소 이력을 강제하지 않는다.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Mapping, Optional


class CoverageStatus(str, Enum):
    SUFFICIENT = "sufficient"        # 필요한 데이터를 모두 충족
    LIMITED = "limited"              # 일부 부족, 제한적 분석 가능 (신뢰도 하향 표시)
    STALE = "stale"                  # 데이터가 오래돼 현재 판단에 부적절
    INSUFFICIENT = "insufficient"    # 핵심 데이터 부족으로 산출 불가


# 여러 입력의 판정을 합칠 때의 우선순위 (나쁜 쪽이 이김)
_SEVERITY = {CoverageStatus.SUFFICIENT: 0, CoverageStatus.LIMITED: 1, CoverageStatus.STALE: 2, CoverageStatus.INSUFFICIENT: 3}


@dataclass(frozen=True)
class InputRequirement:
    """엔진 입력 하나의 요구 조건.

    min_obs: 이보다 적으면 산출 불가(필수 입력) 또는 제한(선택 입력).
    preferred_obs: min_obs 이상 이 값 미만이면 LIMITED (추정이 불안정할 수 있음).
    max_age: 마지막 이용 가능 시각부터 as_of까지 이보다 오래되면 STALE. 거래 세션 기준 판정이 필요한 데이터는
             upstream(4-7 max_staleness 정책)에서 InputSnapshot.stale로 넘긴다.
    required: False면 부족해도 INSUFFICIENT가 아니라 LIMITED (예: 선택 피처).
    """
    name: str
    min_obs: int
    preferred_obs: Optional[int] = None
    max_age: Optional[timedelta] = None
    required: bool = True

    def __post_init__(self):
        if self.min_obs < 0 or (self.preferred_obs is not None and self.preferred_obs < self.min_obs):
            raise ValueError(f"{self.name}: 0 <= min_obs <= preferred_obs 이어야 함")


@dataclass(frozen=True)
class EngineRequirement:
    engine: str
    version: str                     # 요구 조건을 바꾸면 버전을 올리고 이유를 기록한다
    inputs: tuple[InputRequirement, ...]
    note: str = ""


@dataclass(frozen=True)
class InputSnapshot:
    """as_of 시점에 이용 가능했던 입력의 요약. 미래에 들어올 데이터를 세지 않는다."""
    name: str
    n_obs: int
    last_available_at: Optional[datetime] = None
    stale: bool = False              # upstream staleness 정책의 판정 (예: 누락된 거래 세션 수)


@dataclass(frozen=True)
class CoverageReport:
    engine: str
    requirement_version: str
    as_of: datetime
    status: CoverageStatus
    reasons: tuple[str, ...] = field(default_factory=tuple)
    missing_inputs: tuple[str, ...] = field(default_factory=tuple)

    @property
    def can_compute(self) -> bool:
        """엔진이 값을 낼 수 있는가 (LIMITED는 내되 제한을 표시)."""
        return self.status in (CoverageStatus.SUFFICIENT, CoverageStatus.LIMITED)


def _aware(t: datetime, what: str) -> datetime:
    if t.tzinfo is None:
        raise ValueError(f"{what}에 시간대가 없음 — UTC 등 시간대가 있는 시각만 받는다")
    return t


def assess(req: EngineRequirement, snapshots: Mapping[str, InputSnapshot], as_of: datetime) -> CoverageReport:
    """엔진 요구 조건과 as_of 시점의 입력 요약으로 커버리지를 판정한다.

    입력별 판정 → 가장 나쁜 상태가 엔진의 상태. 선택 입력의 부족·노후는 LIMITED까지만 내린다.
    as_of보다 늦게 이용 가능해진 입력이 섞여 있으면 시간 역전이므로 오류를 낸다 (Fail Closed).
    """
    _aware(as_of, "as_of")
    worst, reasons, missing = CoverageStatus.SUFFICIENT, [], []

    def worse(status, reason):
        nonlocal worst
        reasons.append(reason)
        if _SEVERITY[status] > _SEVERITY[worst]:
            worst = status

    for r in req.inputs:
        s = snapshots.get(r.name)
        cap = (lambda st: st) if r.required else (lambda st: CoverageStatus.LIMITED)
        if s is None or s.n_obs < r.min_obs:
            missing.append(r.name)
            have = 0 if s is None else s.n_obs
            worse(cap(CoverageStatus.INSUFFICIENT), f"{r.name}: 관측 {have}개 < 최소 {r.min_obs}개")
            continue
        if s.last_available_at is not None and _aware(s.last_available_at, f"{r.name}.last_available_at") > as_of:
            raise ValueError(f"{r.name}: as_of({as_of}) 이후에 이용 가능해진 데이터가 들어옴 — 시간 역전")
        too_old = s.stale or (r.max_age is not None and s.last_available_at is not None
                              and as_of - s.last_available_at > r.max_age)
        if r.max_age is not None and s.last_available_at is None:
            worse(cap(CoverageStatus.STALE), f"{r.name}: 마지막 이용 가능 시각을 모름")
        elif too_old:
            worse(cap(CoverageStatus.STALE), f"{r.name}: 오래된 데이터 (마지막 {s.last_available_at})")
        if r.preferred_obs is not None and s.n_obs < r.preferred_obs:
            worse(CoverageStatus.LIMITED, f"{r.name}: 관측 {s.n_obs}개 < 권장 {r.preferred_obs}개 (추정 불안정 가능)")
    return CoverageReport(req.engine, req.version, as_of, worst, tuple(reasons), tuple(missing))


# 엔진별 기본 요구 조건 — 초기 가정이다. 실제 데이터로 추정 안정성을 확인해 정하고, 바꾸면 version을 올린다 (가이드 3-6).
_DAY = timedelta(days=1)
DEFAULT_REQUIREMENTS: dict[str, EngineRequirement] = {
    "garch": EngineRequirement("garch", "0.1", (
        InputRequirement("daily_returns", min_obs=500, preferred_obs=750, max_age=7 * _DAY),),
        note="이전 저장소의 학습 3년(약 750거래일)을 권장, 2년 미만은 산출하지 않음"),
    "rolling_beta": EngineRequirement("rolling_beta", "0.1", (
        InputRequirement("stock_returns", min_obs=120, preferred_obs=250, max_age=7 * _DAY),
        InputRequirement("market_returns", min_obs=120, preferred_obs=250, max_age=7 * _DAY))),
    "fair_value_dcf": EngineRequirement("fair_value_dcf", "0.1", (
        InputRequirement("annual_financials", min_obs=3, preferred_obs=5),
        InputRequirement("shares_outstanding", min_obs=1, max_age=200 * _DAY))),
    "financial_health": EngineRequirement("financial_health", "0.1", (
        InputRequirement("quarterly_financials", min_obs=4, preferred_obs=8),),
        note="8분기는 권장값이지 절대 조건이 아님 — 4분기 이상이면 부분 평가"),
    "global_exposure": EngineRequirement("global_exposure", "0.1", (
        InputRequirement("segment_disclosure", min_obs=1, max_age=550 * _DAY),
        InputRequirement("industry_drivers", min_obs=1, required=False))),
    "portfolio_risk": EngineRequirement("portfolio_risk", "0.1", (
        InputRequirement("aligned_returns", min_obs=120, preferred_obs=250, max_age=7 * _DAY),)),
}
