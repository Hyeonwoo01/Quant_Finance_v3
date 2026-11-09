"""PIT 버전 파일 저장소 (가이드 4-2, 6-1).

PITRecord를 JSON Lines로 추가만 한다. 불러오면 core.pit_schema.PITStore가 되어 같은 규칙(권한·정정·as-of)으로 조회한다.

- 같은 revision_id를 다시 받으면: 수집 시각 말고 모두 같으면 건너뛴다 (매일 최근 며칠을 다시 훑어도 중복이 안 쌓임).
  내용이 다르면 덮어쓰지 않고 충돌로 돌려준다 — 공급자가 같은 번호의 값을 바꾼 것이므로 사람이 확인해야 한다.
- 배치 전체를 먼저 검증한 뒤에 쓴다. 하나라도 규칙(권한·정정 대상·시간 역전)을 어기면 아무것도 쓰지 않는다.
- 파일은 시리즈별로 나누고, 시리즈 안에서는 partition 함수로 나눈다 (기본: entity_id). partition은 관측값 키
  (entity·metric·기간)로만 정해야 같은 관측값의 원본과 정정본이 한 파일에 모인다. 추가할 때는 건드리는 파일만 읽는다.

배치: <root>/pit_versions/<series_id>/<partition>.jsonl  (파일 안에서는 추가한 순서 = 원본이 정정본보다 앞)
"""
import json
import re
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping, Optional

from core.data_contracts import ContractRegistry
from core.pit_schema import PITRecord, PITStore

_DATETIME_FIELDS = {f.name for f in fields(PITRecord) if f.name.endswith("_utc")}
_DATE_FIELDS = {"observation_period_start", "observation_period_end", "vintage_date"}
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")

Partition = Callable[[PITRecord], str]


def by_entity(rec: PITRecord) -> str:
    return rec.entity_id


def by_period_month(rec: PITRecord) -> str:
    """관측 기간 시작일의 연-월 (예: 공시 접수일 기준 월별 파일)."""
    if rec.observation_period_start is None:
        raise ValueError(f"{rec.revision_id}: observation_period_start가 없어 월별로 나눌 수 없음")
    return f"{rec.observation_period_start:%Y-%m}"


def record_to_json(rec: PITRecord) -> dict:
    out = {}
    for k, v in asdict(rec).items():
        if isinstance(v, Enum):
            v = v.value
        elif isinstance(v, (datetime, date)):
            v = v.isoformat()
        out[k] = v
    return out


def record_from_json(d: dict) -> PITRecord:
    kw = dict(d)
    for k in _DATETIME_FIELDS:
        if kw.get(k) is not None:
            kw[k] = datetime.fromisoformat(kw[k])
    for k in _DATE_FIELDS:
        if kw.get(k) is not None:
            kw[k] = date.fromisoformat(kw[k])
    return PITRecord(**kw)


def _same_content(a: PITRecord, b: PITRecord) -> bool:
    """수집 시각과 그에 따른 가용 시각만 다르면 같은 레코드로 본다 (다시 받은 같은 버전)."""
    return replace(a, ingested_at_utc=b.ingested_at_utc, available_at_utc=b.available_at_utc) == b


def _safe(name: str, what: str) -> str:
    if not name or not _SAFE_NAME.match(name):
        raise ValueError(f"{what}로 쓸 수 없는 이름: {name!r}")
    return name


@dataclass(frozen=True)
class AppendResult:
    added: int
    unchanged: int
    conflicts: tuple = field(default_factory=tuple)   # 같은 revision_id인데 내용이 다른 것 (쓰지 않음)


class PITFileStore:
    def __init__(self, root, registry: ContractRegistry, partitions: Optional[Mapping[str, Partition]] = None):
        self.root, self.registry = Path(root), registry
        self.partitions = dict(partitions or {})

    def partition_of(self, rec: PITRecord) -> str:
        return _safe(self.partitions.get(rec.series_id, by_entity)(rec), "partition")

    def _path(self, series_id: str, part: str) -> Path:
        return self.root / "pit_versions" / _safe(series_id, "series_id") / f"{_safe(part, 'partition')}.jsonl"

    def series_ids(self) -> list[str]:
        base = self.root / "pit_versions"
        return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.exists() else []

    def _files(self, series_ids=None, parts: Optional[Iterable[tuple]] = None) -> list[Path]:
        if parts is not None:
            return [p for p in (self._path(s, k) for s, k in sorted(set(parts))) if p.exists()]
        sids = sorted(series_ids) if series_ids is not None else self.series_ids()
        return [p for s in sids for p in sorted((self.root / "pit_versions" / _safe(s, "series_id")).glob("*.jsonl"))]

    def iter_records(self, series_ids: Optional[Iterable[str]] = None, *,
                     parts: Optional[Iterable[tuple]] = None) -> Iterator[PITRecord]:
        """series_ids의 모든 파일, 또는 parts=[(series_id, partition), …]에 해당하는 파일만."""
        for p in self._files(series_ids, parts):
            with p.open(encoding="utf-8") as f:
                for line in f:
                    yield record_from_json(json.loads(line))

    def load(self, series_ids: Optional[Iterable[str]] = None, *, parts: Optional[Iterable[tuple]] = None) -> PITStore:
        store = PITStore(self.registry)
        for rec in self.iter_records(series_ids, parts=parts):
            store.append(rec)
        return store

    def load_for(self, records: Iterable[PITRecord]) -> PITStore:
        """이 레코드들이 들어갈 파일만 읽은 저장소 (정정 대상 찾기·중복 확인용)."""
        return self.load(parts={(r.series_id, self.partition_of(r)) for r in records})

    def append(self, records: Iterable[PITRecord], store: Optional[PITStore] = None) -> AppendResult:
        """store를 주면 그 위에서 검증하고 그 store에도 반영한다 (같은 실행 안에서 여러 번 추가할 때 다시 읽지 않게)."""
        records = list(records)
        store = store if store is not None else self.load_for(records)
        new, unchanged, conflicts = [], 0, []
        for r in records:
            old = store.revision(r.revision_id)
            if old is not None:
                if _same_content(old, r):
                    unchanged += 1
                else:
                    conflicts.append(r.revision_id)
                continue
            store.append(r)          # 권한·정정 규칙 검증 (실패하면 예외, 아직 아무것도 쓰지 않음)
            new.append(r)
        by_path: dict[Path, list[str]] = {}
        for r in new:
            by_path.setdefault(self._path(r.series_id, self.partition_of(r)), []).append(
                json.dumps(record_to_json(r), ensure_ascii=False, sort_keys=True))
        for path, lines in sorted(by_path.items()):
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        return AppendResult(len(new), unchanged, tuple(conflicts))
