"""데이터 계약과 목적별 권한 게이트: 확인하지 못한 것은 막는다 (가이드 3-1 ~ 3-5)."""
from datetime import datetime

import pytest

from core.data_contracts import ApprovalStatus, ContractRegistry, PermissionDenied, Purpose, permission_fields
from tests.conftest import make_contract


def test_unknown_and_unreviewed_contracts_are_blocked_for_every_purpose(registry):
    for p in Purpose:
        assert not registry.is_allowed("unreviewed", p)
        assert not registry.is_allowed("no_such_contract", p)
    with pytest.raises(PermissionDenied):
        registry.require("no_such_contract", Purpose.STORAGE)


def test_each_permission_is_checked_independently(registry):
    assert registry.is_allowed("kr_price", Purpose.ML_TRAINING) and registry.is_allowed("kr_price", Purpose.STORAGE)
    assert not registry.is_allowed("kr_price", Purpose.LLM_INPUT)            # 학습이 허용돼도 LLM 입력은 따로
    assert not registry.is_allowed("kr_price", Purpose.RAW_REDISTRIBUTION)
    assert registry.is_allowed("store_only", Purpose.STORAGE) and not registry.is_allowed("store_only", Purpose.ML_TRAINING)
    with pytest.raises(PermissionDenied, match="ML_TRAINING"):
        registry.require("store_only", Purpose.ML_TRAINING)


def test_blocked_status_overrides_permission_flags():
    c = make_contract("blocked", status=ApprovalStatus.BLOCKED, allow=(Purpose.STORAGE,))
    assert not c.allows(Purpose.STORAGE)


def test_unreviewed_contract_cannot_claim_permissions():
    with pytest.raises(ValueError, match="검토 전"):
        make_contract("x", status=ApprovalStatus.NOT_REVIEWED, allow=(Purpose.STORAGE,))


def test_open_status_needs_review_timestamp_with_timezone():
    with pytest.raises(ValueError, match="license_reviewed_at"):
        make_contract("x", license_reviewed_at=None)
    with pytest.raises(ValueError, match="시간대"):
        make_contract("x", license_reviewed_at=datetime(2026, 11, 9))


def test_fred_needs_written_permission_for_storage_and_training():
    with pytest.raises(ValueError, match="FRED"):
        make_contract("fred_dgs10", provider="FRED", allow=(Purpose.ML_TRAINING,))
    with pytest.raises(ValueError, match="FRED"):
        make_contract("alfred_cpi", provider="alfred", allow=(Purpose.CACHING,))
    # 단순 표시는 그 자체로 막지 않음 (약관·출처 표시는 따로 확인) / 서면 허가 근거가 있으면 허용
    assert make_contract("fred_view", provider="FRED", allow=(Purpose.PUBLIC_DISPLAY,)).allows(Purpose.PUBLIC_DISPLAY)
    assert make_contract("fred_ok", provider="FRED", allow=(Purpose.ML_TRAINING,),
                         permission_reference="letter-2026-11").allows(Purpose.ML_TRAINING)


def test_registry_rejects_duplicate_ids_and_lists_permission_fields(registry):
    with pytest.raises(ValueError, match="이미 등록"):
        registry.register(make_contract("kr_price"))
    assert len(registry) == 3
    assert permission_fields() == [p.value for p in Purpose]
