"""종목 식별자의 시점별 매핑 (가이드 4-8).

내부 식별자(entity_id, 지금은 DART 고유번호)와 KRX 종목코드를 "그 시점에 유효했던" 관계로 잇는다.
매핑 행에는 유효 시작·종료 시각과 출처(계약 ID)가 있고, 바뀌면 새 행을 더하며 과거 행은 고치지 않는다.

- 유효 시작은 우리가 그 매핑을 처음 쓸 수 있게 된 시각(available_at)이다. 수집을 시작하기 전의 매핑은
  알 수 없으므로, 그 전 시점을 물으면 BEFORE_COVERAGE로 답한다. 지금 종목코드로 과거 데이터를 묶지 않는다.
- 종목코드는 다른 회사가 다시 쓸 수 있다. 같은 시점에 두 회사가 같은 코드를 가지면 AMBIGUOUS로 답하고 고르지 않는다.
- ISIN·해외 티커 같은 다른 식별자는 그 공급원의 계약을 등록한 뒤 같은 구조로 더한다.
"""
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable, Optional

from core.pit_schema import PITRecord


class MapStatus(str, Enum):
    OK = "ok"
    NO_CODE = "no_code"                      # 그 시점에 그 회사에 종목코드가 없었다 (또는 그 코드를 쓰는 회사가 없었다)
    BEFORE_COVERAGE = "before_coverage"      # 매핑 수집을 시작하기 전 시점이라 알 수 없다
    UNKNOWN = "unknown"                      # 매핑 표에 없는 식별자
    AMBIGUOUS = "ambiguous"                  # 같은 시점에 그 종목코드를 가진 회사가 둘 이상


@dataclass(frozen=True)
class Mapping:
    entity_id: str
    stock_code: str                  # 빈 문자열 = 그 기간에 종목코드 없음
    name: str
    valid_from_utc: datetime         # 이 매핑을 쓸 수 있게 된 시각 (포함)
    valid_to_utc: Optional[datetime]  # 다음 버전의 시작 시각 (미포함). None이면 지금도 유효
    source_contract_id: str
    revision_id: str

    def active(self, as_of: datetime) -> bool:
        return self.valid_from_utc <= as_of and (self.valid_to_utc is None or as_of < self.valid_to_utc)


@dataclass(frozen=True)
class Resolution:
    status: MapStatus
    mapping: Optional[Mapping] = None
    candidates: tuple = ()           # AMBIGUOUS일 때 후보 매핑

    @property
    def ok(self) -> bool:
        return self.status is MapStatus.OK


def _aware(t: datetime) -> datetime:
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError("as_of에 시간대가 없음 — 시간대가 있는 시각만 받는다")
    return t.astimezone(timezone.utc)


class SecurityMaster:
    def __init__(self, mappings: Iterable[Mapping]):
        self._by_entity: dict[str, list[Mapping]] = {}
        self._by_code: dict[str, list[Mapping]] = {}
        for m in sorted(mappings, key=lambda m: (m.entity_id, m.valid_from_utc)):
            self._by_entity.setdefault(m.entity_id, []).append(m)
            if m.stock_code:
                self._by_code.setdefault(m.stock_code, []).append(m)
        starts = [ms[0].valid_from_utc for ms in self._by_entity.values()]
        self.coverage_start_utc: Optional[datetime] = min(starts) if starts else None

    @classmethod
    def from_records(cls, records: Iterable[PITRecord]) -> "SecurityMaster":
        """회사별 버전 레코드(값: {"corp_name", "stock_code"} JSON)를 유효 구간으로 바꾼다."""
        by_entity: dict[str, list[PITRecord]] = {}
        for r in records:
            by_entity.setdefault(r.entity_id, []).append(r)
        mappings = []
        for entity, recs in by_entity.items():
            recs.sort(key=lambda r: (r.available_at_utc, r.ingested_at_utc))
            for i, r in enumerate(recs):
                d = json.loads(r.value)
                nxt = recs[i + 1].available_at_utc if i + 1 < len(recs) else None
                if nxt is not None and nxt == r.available_at_utc:      # 같은 시각에 둘이면 뒤의 것이 유효
                    continue
                mappings.append(Mapping(entity, d["stock_code"], d["corp_name"], r.available_at_utc, nxt,
                                        r.license_contract_id, r.revision_id))
        return cls(mappings)

    def __len__(self) -> int:
        return len(self._by_entity)

    def history(self, entity_id: str) -> list[Mapping]:
        return list(self._by_entity.get(entity_id, []))

    def _before_coverage(self, as_of: datetime) -> bool:
        return self.coverage_start_utc is None or as_of < self.coverage_start_utc

    def code_of(self, entity_id: str, as_of_utc: datetime) -> Resolution:
        """그 시점에 그 회사가 쓰던 종목코드."""
        as_of = _aware(as_of_utc)
        if self._before_coverage(as_of):
            return Resolution(MapStatus.BEFORE_COVERAGE)
        ms = self._by_entity.get(entity_id)
        if not ms:
            return Resolution(MapStatus.UNKNOWN)
        for m in ms:
            if m.active(as_of):
                return Resolution(MapStatus.OK if m.stock_code else MapStatus.NO_CODE, m)
        return Resolution(MapStatus.NO_CODE)          # 표에 처음 나타나기 전: 그때는 종목코드가 없었다

    def entity_of(self, stock_code: str, as_of_utc: datetime) -> Resolution:
        """그 시점에 그 종목코드를 쓰던 회사."""
        as_of = _aware(as_of_utc)
        if self._before_coverage(as_of):
            return Resolution(MapStatus.BEFORE_COVERAGE)
        ms = self._by_code.get(stock_code)
        if not ms:
            return Resolution(MapStatus.UNKNOWN)
        active = [m for m in ms if m.active(as_of)]
        if len(active) == 1:
            return Resolution(MapStatus.OK, active[0])
        if not active:
            return Resolution(MapStatus.NO_CODE)
        return Resolution(MapStatus.AMBIGUOUS, candidates=tuple(active))
