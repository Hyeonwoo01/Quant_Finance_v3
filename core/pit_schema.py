"""Point-in-Time 레코드와 as-of 조회 (가이드 4-1, 4-2).

관측 기간·발표 시각·빈티지·공급자 가용 시각·수집 시각은 서로 다른 의미라 따로 저장한다. 백테스트와 모델은 available_at_utc
(보수적 가용 시각)만 보고, 같은 관측값의 정정은 새 revision으로 덧붙인다 — 과거 버전을 덮어쓰지 않는다.
지금은 메모리 저장소이며, Parquet·DuckDB 저장은 같은 인터페이스로 뒤에 붙인다 (가이드 6장).
"""
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional, Union

import pandas as pd

from core.data_contracts import ContractRegistry, Purpose

Value = Union[float, int, str, bool, None]


class QualityStatus(str, Enum):
    VALID = "valid"          # 사용 가능
    SUSPECT = "suspect"      # 확인 중 (기본 조회에서 제외)
    INVALID = "invalid"      # 오류로 확인됨 (조회에서 제외, 기록은 보존)


def _utc(t: Optional[datetime], name: str) -> Optional[datetime]:
    """시간대가 있는 시각만 받아 UTC로 바꾼다. 시간대 없는 시각은 거부 (어느 시간대인지 추측하지 않는다)."""
    if t is None:
        return None
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError(f"{name}에 시간대가 없음 — 시간대가 있는 시각만 받는다")
    return t.astimezone(timezone.utc)


@dataclass(frozen=True)
class PITRecord:
    entity_id: str
    series_id: str
    metric_id: str
    available_at_utc: datetime          # 백테스트·모델이 쓰는 보수적 가용 시각 (필수)
    ingested_at_utc: datetime           # 우리 시스템이 실제로 수집한 시각 (필수)
    revision_id: str
    value: Value
    unit: str
    source_provider: str
    raw_payload_hash: str
    license_contract_id: str
    observation_period_start: Optional[date] = None
    observation_period_end: Optional[date] = None
    event_time_utc: Optional[datetime] = None
    official_release_at_utc: Optional[datetime] = None
    vintage_date: Optional[date] = None
    provider_available_at_utc: Optional[datetime] = None
    supersedes_revision_id: Optional[str] = None
    currency: Optional[str] = None
    source_document_id: Optional[str] = None
    source_url: Optional[str] = None
    quality_status: QualityStatus = QualityStatus.VALID

    def __post_init__(self):
        for name in ("available_at_utc", "ingested_at_utc", "event_time_utc", "official_release_at_utc",
                     "provider_available_at_utc"):
            object.__setattr__(self, name, _utc(getattr(self, name), name))
        if self.available_at_utc is None or self.ingested_at_utc is None:
            raise ValueError("available_at_utc와 ingested_at_utc는 필수")
        # 가용 시각은 공식 발표·공급자 가용 시각보다 앞설 수 없다 (보수적 규칙). 빈티지 날짜를 복사해 만들지 않는다.
        for name in ("official_release_at_utc", "provider_available_at_utc"):
            t = getattr(self, name)
            if t is not None and self.available_at_utc < t:
                raise ValueError(f"available_at_utc({self.available_at_utc})가 {name}({t})보다 이름 — 시간 역전")
        if (self.observation_period_start and self.observation_period_end
                and self.observation_period_start > self.observation_period_end):
            raise ValueError("observation_period_start > observation_period_end")
        object.__setattr__(self, "quality_status", QualityStatus(self.quality_status))

    @property
    def observation_key(self) -> tuple:
        """같은 관측값(정정 전후가 같은 값의 다른 버전)을 묶는 키."""
        return (self.entity_id, self.series_id, self.metric_id, self.observation_period_start,
                self.observation_period_end, self.event_time_utc)


class PITStore:
    """추가만 하는 PIT 저장소. 정정은 supersedes_revision_id로 이전 버전을 가리키는 새 레코드로 넣는다."""

    def __init__(self, registry: ContractRegistry):
        self.registry = registry
        self._records: list[PITRecord] = []
        self._revisions: dict[str, PITRecord] = {}

    def append(self, rec: PITRecord) -> None:
        if rec.revision_id in self._revisions:
            raise ValueError(f"이미 있는 revision_id: {rec.revision_id} (과거 버전은 덮어쓰지 않는다)")
        if not self.registry.is_allowed(rec.license_contract_id, Purpose.STORAGE):
            raise PermissionError(f"{rec.license_contract_id}: 저장 권한이 승인되지 않은 데이터는 보관하지 않는다")
        if rec.supersedes_revision_id is not None:
            prev = self._revisions.get(rec.supersedes_revision_id)
            if prev is None:
                raise ValueError(f"supersedes_revision_id가 없는 revision을 가리킴: {rec.supersedes_revision_id}")
            if prev.observation_key != rec.observation_key:
                raise ValueError("정정 레코드는 같은 관측값(entity·series·metric·기간·사건 시각)을 가리켜야 함")
            if rec.available_at_utc < prev.available_at_utc:
                raise ValueError("정정본이 원본보다 먼저 이용 가능했을 수 없음")
        self._records.append(rec)
        self._revisions[rec.revision_id] = rec

    def __len__(self):
        return len(self._records)

    def query_as_of(self, as_of_utc: datetime, purpose: Purpose, *, entity_id: Optional[str] = None,
                    series_id: Optional[str] = None, include_suspect: bool = False) -> pd.DataFrame:
        """as_of 시점에 그 목적으로 쓸 수 있었던 값 (가이드 4-2 PIT 조회 규칙 1~5).

        1) available_at <= as_of  2) 계약이 그 목적을 승인  3) 품질이 사용 가능  4) 같은 관측값의 여러 버전 중
        as_of에 이용 가능했던 최신 버전만  5) 원본은 그대로 (조회는 읽기만 한다).
        """
        as_of = _utc(as_of_utc, "as_of_utc")
        ok_quality = {QualityStatus.VALID} | ({QualityStatus.SUSPECT} if include_suspect else set())
        latest: dict[tuple, PITRecord] = {}
        for r in self._records:
            if r.available_at_utc > as_of or r.quality_status not in ok_quality:
                continue
            if entity_id is not None and r.entity_id != entity_id or series_id is not None and r.series_id != series_id:
                continue
            if not self.registry.is_allowed(r.license_contract_id, purpose):
                continue
            k = r.observation_key
            cur = latest.get(k)
            if cur is None or (r.available_at_utc, r.ingested_at_utc) > (cur.available_at_utc, cur.ingested_at_utc):
                latest[k] = r
        rows = [asdict(r) for r in latest.values()]
        cols = list(PITRecord.__dataclass_fields__)
        out = pd.DataFrame(rows, columns=cols)
        return out.sort_values(["entity_id", "series_id", "metric_id", "available_at_utc"], ignore_index=True)
