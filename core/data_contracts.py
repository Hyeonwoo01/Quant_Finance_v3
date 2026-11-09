"""데이터 공급원 계약과 사용 목적별 권한 게이트 (가이드 3-1 ~ 3-5).

모든 데이터 시리즈는 수집기를 만들기 전에 계약으로 등록한다. 권한은 목적(저장·캐싱·보관·ML 학습·LLM 입력·공개 표시·
원본 재배포·파생 데이터 사용)마다 따로 확인하고, 확인하지 못한 것은 막는다 (Fail Closed).
"""
from dataclasses import dataclass, fields
from datetime import datetime
from enum import Enum
from typing import Optional


class ApprovalStatus(str, Enum):
    NOT_REVIEWED = "not_reviewed"
    RESEARCH_ONLY = "research_only"
    APPROVED_FOR_INTERNAL_USE = "approved_for_internal_use"
    APPROVED_FOR_TRAINING = "approved_for_training"
    APPROVED_FOR_PUBLIC_DISPLAY = "approved_for_public_display"
    APPROVED_FOR_REDISTRIBUTION = "approved_for_redistribution"
    BLOCKED = "blocked"


class Purpose(str, Enum):
    """데이터를 쓰려는 목적. 목적마다 계약의 권한 필드 하나에 대응한다."""
    STORAGE = "storage_allowed"
    CACHING = "caching_allowed"
    ARCHIVAL = "archival_allowed"
    ML_TRAINING = "ml_training_allowed"
    LLM_INPUT = "llm_input_allowed"
    PUBLIC_DISPLAY = "public_display_allowed"
    RAW_REDISTRIBUTION = "raw_redistribution_allowed"
    DERIVED_USE = "derived_data_use_allowed"


# 권한 검토 전이거나 차단된 계약은 어떤 목적에도 쓸 수 없다
_CLOSED = {ApprovalStatus.NOT_REVIEWED, ApprovalStatus.BLOCKED}
# FRED/ALFRED: 현재 약관상 저장·캐싱·보관·ML 학습은 별도 서면 허가가 있을 때만 (가이드 3-2)
_FRED_PROVIDERS = {"FRED", "ALFRED"}
_FRED_RESTRICTED = (Purpose.STORAGE, Purpose.CACHING, Purpose.ARCHIVAL, Purpose.ML_TRAINING)


class PermissionDenied(Exception):
    """계약이 없거나 그 목적의 사용이 승인되지 않았을 때."""


@dataclass(frozen=True)
class DataSourceContract:
    contract_id: str
    provider: str
    source_id: str
    series_or_endpoint: str
    frequency: str
    unit: str
    currency: Optional[str]
    native_timezone: str
    availability_policy_id: str
    revision_policy: str

    # 권한은 각각 독립적으로 확인한다. 기본값을 두지 않는다 (추정해서 True로 두지 않기 위해).
    storage_allowed: bool
    caching_allowed: bool
    archival_allowed: bool
    ml_training_allowed: bool
    llm_input_allowed: bool
    public_display_allowed: bool
    raw_redistribution_allowed: bool
    derived_data_use_allowed: bool

    approval_status: ApprovalStatus
    license_url: str
    license_reviewed_at: Optional[datetime]
    permission_reference: Optional[str]
    attribution_required: bool
    fallback_source_id: Optional[str]
    max_staleness_policy_id: str

    def __post_init__(self):
        status = ApprovalStatus(self.approval_status)
        granted = [p for p in Purpose if getattr(self, p.value)]
        if status is ApprovalStatus.NOT_REVIEWED and granted:
            raise ValueError(f"{self.contract_id}: 검토 전 계약에 권한을 줄 수 없음 ({', '.join(p.name for p in granted)})")
        if status not in _CLOSED and self.license_reviewed_at is None:
            raise ValueError(f"{self.contract_id}: 승인 상태인데 약관 검토 시각(license_reviewed_at)이 없음")
        if self.license_reviewed_at is not None and self.license_reviewed_at.tzinfo is None:
            raise ValueError(f"{self.contract_id}: license_reviewed_at에 시간대가 없음")
        if self.provider.upper() in _FRED_PROVIDERS and not self.permission_reference:
            bad = [p.name for p in _FRED_RESTRICTED if getattr(self, p.value)]
            if bad:
                raise ValueError(f"{self.contract_id}: FRED/ALFRED는 서면 허가(permission_reference) 없이 {', '.join(bad)} 불가")

    def allows(self, purpose: Purpose) -> bool:
        """그 목적의 사용이 승인됐는지. 상태가 열려 있고 그 목적의 권한 필드가 True일 때만."""
        return ApprovalStatus(self.approval_status) not in _CLOSED and bool(getattr(self, Purpose(purpose).value))


class ContractRegistry:
    """계약 목록. 없는 계약은 어떤 목적에도 허용하지 않는다."""

    def __init__(self, contracts=()):
        self._contracts: dict[str, DataSourceContract] = {}
        for c in contracts:
            self.register(c)

    def register(self, contract: DataSourceContract) -> None:
        if contract.contract_id in self._contracts:
            raise ValueError(f"이미 등록된 계약: {contract.contract_id} (바꾸려면 새 contract_id로 등록)")
        self._contracts[contract.contract_id] = contract

    def get(self, contract_id: str) -> Optional[DataSourceContract]:
        return self._contracts.get(contract_id)

    def is_allowed(self, contract_id: str, purpose: Purpose) -> bool:
        c = self._contracts.get(contract_id)
        return c is not None and c.allows(purpose)

    def require(self, contract_id: str, purpose: Purpose) -> DataSourceContract:
        """허용되면 계약을, 아니면 PermissionDenied. 수집·저장·학습 코드는 이 함수로 먼저 확인한다."""
        c = self._contracts.get(contract_id)
        if c is None:
            raise PermissionDenied(f"등록되지 않은 계약: {contract_id}")
        if not c.allows(purpose):
            raise PermissionDenied(f"{contract_id}: {Purpose(purpose).name} 사용이 승인되지 않음 (상태 {ApprovalStatus(c.approval_status).value})")
        return c

    def __len__(self):
        return len(self._contracts)


def permission_fields() -> list[str]:
    """권한 필드 이름 목록 (계약 표·문서 생성용)."""
    names = {p.value for p in Purpose}
    return [f.name for f in fields(DataSourceContract) if f.name in names]
