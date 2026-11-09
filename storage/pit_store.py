"""PIT 버전 파일 저장소 (가이드 4-2, 6-1).

PITRecord를 JSON Lines로 추가만 한다. 불러오면 core.pit_schema.PITStore가 되어 같은 규칙(권한·정정·as-of)으로 조회한다.

- 같은 revision_id를 다시 받으면: 수집 시각 말고 모두 같으면 건너뛴다 (매일 최근 며칠을 다시 훑어도 중복이 안 쌓임).
  내용이 다르면 덮어쓰지 않고 충돌로 돌려준다 — 공급자가 같은 번호의 값을 바꾼 것이므로 사람이 확인해야 한다.
- 배치 전체를 먼저 검증한 뒤에 쓴다. 하나라도 규칙(권한·정정 대상·시간 역전)을 어기면 아무것도 쓰지 않는다.

배치: <root>/pit_versions/<series_id>/<available_at UTC YYYY-MM>.jsonl
정정본은 원본보다 늦게 이용 가능하므로 같은 달 파일의 뒤쪽이나 이후 달 파일에 놓여, 파일 순서대로 읽으면 원본이 먼저 온다.
"""
import json
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Iterable, Iterator, Optional

from core.data_contracts import ContractRegistry
from core.pit_schema import PITRecord, PITStore

_DATETIME_FIELDS = {f.name for f in fields(PITRecord) if f.name.endswith("_utc")}
_DATE_FIELDS = {"observation_period_start", "observation_period_end", "vintage_date"}


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
    """수집 시각만 다르면 같은 레코드로 본다."""
    return replace(a, ingested_at_utc=b.ingested_at_utc) == b


@dataclass(frozen=True)
class AppendResult:
    added: int
    unchanged: int
    conflicts: tuple = field(default_factory=tuple)   # 같은 revision_id인데 내용이 다른 것 (쓰지 않음)


class PITFileStore:
    def __init__(self, root, registry: ContractRegistry):
        self.root, self.registry = Path(root), registry

    def _dir(self, series_id: str) -> Path:
        if not series_id or "/" in series_id or series_id.startswith("."):
            raise ValueError(f"series_id로 쓸 수 없는 이름: {series_id!r}")
        return self.root / "pit_versions" / series_id

    def series_ids(self) -> list[str]:
        base = self.root / "pit_versions"
        return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.exists() else []

    def iter_records(self, series_ids: Optional[Iterable[str]] = None) -> Iterator[PITRecord]:
        for sid in (sorted(series_ids) if series_ids is not None else self.series_ids()):
            for p in sorted(self._dir(sid).glob("*.jsonl")):
                with p.open(encoding="utf-8") as f:
                    for line in f:
                        yield record_from_json(json.loads(line))

    def load(self, series_ids: Optional[Iterable[str]] = None) -> PITStore:
        store = PITStore(self.registry)
        for rec in self.iter_records(series_ids):
            store.append(rec)
        return store

    def append(self, records: Iterable[PITRecord]) -> AppendResult:
        records = list(records)
        store = self.load({r.series_id for r in records})
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
            path = self._dir(r.series_id) / f"{r.available_at_utc:%Y-%m}.jsonl"
            by_path.setdefault(path, []).append(json.dumps(record_to_json(r), ensure_ascii=False, sort_keys=True))
        for path, lines in by_path.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        return AppendResult(len(new), unchanged, tuple(conflicts))
