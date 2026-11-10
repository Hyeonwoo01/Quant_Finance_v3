"""저장소가 공개인지 비공개인지 (가이드 3-1, DATA_SOURCES.md '한국 주식 가격').

수집한 데이터는 공개 GitHub 저장소의 data 브랜치에 쌓인다. 공개 저장소에 올리는 것은 제3자 제공·재배포이므로,
재배포가 금지된 공급원(예: 한국거래소 원천 시세)은 비공개 저장소에만 둘 수 있다.

- 저장소 루트에 PRIVATE_STORE 파일이 있을 때만 비공개로 본다. 없으면 공개로 본다 (확인 못 하면 막는 쪽).
- 공개 저장소에는 원본 응답은 '원본 재배포', 정규화한 값은 '공개 표시'가 승인된 계약만 쓸 수 있다.
- 표식 파일은 사람이 비공개 저장소에 직접 두거나, 워크플로가 그 저장소가 비공개임을 GitHub에 확인한 뒤에만 만든다.
"""
from pathlib import Path

from core.data_contracts import ContractRegistry, PermissionDenied, Purpose

PRIVATE_MARKER = "PRIVATE_STORE"


def is_private_store(root) -> bool:
    return (Path(root) / PRIVATE_MARKER).is_file()


def require_placement(registry: ContractRegistry, contract_id: str, root, purpose_if_public: Purpose) -> None:
    """공개 저장소에 쓰려면 그 목적(원본 재배포 또는 공개 표시)이 승인돼 있어야 한다. 아니면 PermissionDenied."""
    if is_private_store(root):
        return
    if not registry.is_allowed(contract_id, purpose_if_public):
        raise PermissionDenied(
            f"{contract_id}: {purpose_if_public.name}이 승인되지 않은 데이터는 공개 저장소({root})에 둘 수 없음 — "
            f"비공개 저장소({PRIVATE_MARKER} 표식이 있는 곳)에만 저장")
