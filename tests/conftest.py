"""테스트가 함께 쓰는 계약 생성기."""
from datetime import datetime, timezone

import pytest

from core.data_contracts import ApprovalStatus, ContractRegistry, DataSourceContract, Purpose

REVIEWED = datetime(2026, 11, 9, tzinfo=timezone.utc)


def make_contract(contract_id="kr_price", status=ApprovalStatus.APPROVED_FOR_TRAINING, allow=(), provider="KRX", **kw):
    """allow에 준 목적만 True인 계약. 상태가 열려 있으면 검토 시각을 채운다."""
    perms = {p.value: (p in allow) for p in Purpose}
    closed = status in (ApprovalStatus.NOT_REVIEWED, ApprovalStatus.BLOCKED)
    base = dict(contract_id=contract_id, provider=provider, source_id="src", series_or_endpoint="daily", frequency="1D",
                unit="KRW", currency="KRW", native_timezone="Asia/Seoul", availability_policy_id="kr_close",
                revision_policy="append", approval_status=status, license_url="https://example.invalid/terms",
                license_reviewed_at=None if closed else REVIEWED, permission_reference=None, attribution_required=True,
                fallback_source_id=None, max_staleness_policy_id="daily_sessions")
    base.update(perms)
    base.update(kw)
    return DataSourceContract(**base)


@pytest.fixture
def registry():
    """저장·학습 가능한 가격 계약, 저장만 가능한 계약, 검토 전 계약."""
    return ContractRegistry([
        make_contract("kr_price", allow=(Purpose.STORAGE, Purpose.ML_TRAINING)),
        make_contract("store_only", status=ApprovalStatus.APPROVED_FOR_INTERNAL_USE, allow=(Purpose.STORAGE,)),
        make_contract("unreviewed", status=ApprovalStatus.NOT_REVIEWED),
    ])
