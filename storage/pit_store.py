"""PIT 버전 파일 저장소 (가이드 4-2, 6-1).

PITRecord를 JSON Lines로 추가만 한다. 불러오면 core.pit_schema.PITStore가 되어 같은 규칙(권한·정정·as-of)으로 조회한다.

- 같은 revision_id를 다시 받으면: 수집 시각 말고 모두 같으면 건너뛴다 (매일 최근 며칠을 다시 훑어도 중복이 안 쌓임).
  내용이 다르면 덮어쓰지 않고 충돌로 돌려준다 — 공급자가 같은 번호의 값을 바꾼 것이므로 사람이 확인해야 한다.
- 배치 전체를 먼저 검증한 뒤에 쓴다. 하나라도 규칙(권한·정정 대상·시간 역전)을 어기면 아무것도 쓰지 않는다.
- 파일은 시리즈별로 나누고, 시리즈 안에서는 partition 함수로 나눈다 (기본: entity_id). partition은 관측값 키
  (entity·metric·기간)로만 정해야 같은 관측값의 원본과 정정본이 한 파일에 모인다. 추가할 때는 건드리는 파일만 읽는다.

배치: <root>/pit_versions/<series_id>/<partition>.jsonl  (파일 안에서는 추가한 순서 = 원본이 정정본보다 앞)
     <root>/pit_versions/<series_id>/<partition>.parquet  (parquet_series로 지정한 시리즈: 같은 레코드·같은 규칙, 열 압축)

Parquet: 하루 수천 행씩 쌓이는 시리즈(일별 시세)는 JSON Lines로는 너무 커서(16년이면 10GB 이상) Parquet(zstd)로 쓴다.
레코드 필드와 정정 규칙은 같고 파일 형식만 다르다. 값(value)은 형식을 그대로 보존하도록 JSON 문자열로 넣는다.
Parquet는 덧붙이기가 안 되므로 그 파티션 파일을 (기존 + 새 레코드) 순서 그대로 다시 쓴다 (임시 파일 → 교체).
아직 JSON Lines로 남은 파티션은 처음 쓸 때 Parquet로 옮기고 원래 파일을 지운다. 한꺼번에 옮기려면 migrate_to_parquet.
"""
import json
import os
import re
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping, Optional

from core.data_contracts import ContractRegistry, Purpose
from core.pit_schema import PITRecord, PITStore
from storage.visibility import require_placement

_DATETIME_FIELDS = {f.name for f in fields(PITRecord) if f.name.endswith("_utc")}
_DATE_FIELDS = {"observation_period_start", "observation_period_end", "vintage_date"}
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")

Partition = Callable[[PITRecord], str]


def by_entity(rec: PITRecord) -> str:
    return rec.entity_id


def by_period_day(rec: PITRecord) -> str:
    """관측 기간 시작일 (예: 하루에 수천 종목이 생기는 일별 시세는 날짜별 파일)."""
    if rec.observation_period_start is None:
        raise ValueError(f"{rec.revision_id}: observation_period_start가 없어 날짜별로 나눌 수 없음")
    return f"{rec.observation_period_start:%Y-%m-%d}"


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
    """수집 시각, 그에 따른 가용 시각, 원본 응답 해시만 다르면 같은 레코드로 본다 (다시 받은 같은 버전).
    원본 응답 해시는 같은 값이라도 받을 때마다 달라질 수 있다 — 예: 공시 목록은 조회 기간이 하루씩 밀리면
    같은 공시가 다른 내용의 페이지에 실려 온다."""
    return replace(a, ingested_at_utc=b.ingested_at_utc, available_at_utc=b.available_at_utc,
                   raw_payload_hash=b.raw_payload_hash) == b


def _safe(name: str, what: str) -> str:
    if not name or not _SAFE_NAME.match(name):
        raise ValueError(f"{what}로 쓸 수 없는 이름: {name!r}")
    return name


@dataclass(frozen=True)
class AppendResult:
    added: int
    unchanged: int
    conflicts: tuple = field(default_factory=tuple)   # 같은 revision_id인데 내용이 다른 것 (쓰지 않음)


_PARQUET_FIELDS = [f.name for f in fields(PITRecord)]


def _parquet_schema():
    import pyarrow as pa
    cols = []
    for name in _PARQUET_FIELDS:
        if name in _DATETIME_FIELDS:
            cols.append(pa.field(name, pa.timestamp("us", tz="UTC")))
        elif name in _DATE_FIELDS:
            cols.append(pa.field(name, pa.date32()))
        elif name == "value":
            cols.append(pa.field("value_json", pa.string()))     # 숫자·문자열·참거짓·없음을 형식 그대로 보존
        else:
            cols.append(pa.field(name, pa.string()))
    return pa.schema(cols)


def write_parquet(path: Path, records: list) -> None:
    """레코드를 그 순서 그대로 Parquet로 쓴다 (임시 파일에 쓰고 교체해 중간 상태가 남지 않게)."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    data = {c: [] for c in _parquet_schema().names}
    for r in records:
        d = asdict(r)
        for name in _PARQUET_FIELDS:
            v = d[name]
            if name == "value":
                data["value_json"].append(json.dumps(v, ensure_ascii=False))
            elif isinstance(v, Enum):
                data[name].append(v.value)
            else:
                data[name].append(v)
    table = pa.Table.from_pydict(data, schema=_parquet_schema())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(table, tmp, compression="zstd", use_dictionary=True, write_statistics=False)
    os.replace(tmp, path)


def read_parquet(path: Path) -> list:
    import pyarrow.parquet as pq
    rows = pq.read_table(path).to_pylist()
    out = []
    for row in rows:
        row["value"] = json.loads(row.pop("value_json"))
        out.append(PITRecord(**row))
    return out


class PITFileStore:
    def __init__(self, root, registry: ContractRegistry, partitions: Optional[Mapping[str, Partition]] = None,
                 parquet_series: Iterable[str] = ()):
        self.root, self.registry = Path(root), registry
        self.partitions = dict(partitions or {})
        self.parquet_series = frozenset(parquet_series)

    def partition_of(self, rec: PITRecord) -> str:
        return _safe(self.partitions.get(rec.series_id, by_entity)(rec), "partition")

    def _suffix(self, series_id: str) -> str:
        return ".parquet" if series_id in self.parquet_series else ".jsonl"

    def _path(self, series_id: str, part: str, suffix: Optional[str] = None) -> Path:
        return (self.root / "pit_versions" / _safe(series_id, "series_id")
                / f"{_safe(part, 'partition')}{suffix or self._suffix(series_id)}")

    def series_ids(self) -> list[str]:
        base = self.root / "pit_versions"
        return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.exists() else []

    def _existing(self, series_id: str, part: str) -> Optional[Path]:
        """그 파티션의 파일. Parquet 시리즈는 Parquet를 먼저 보고, 아직 안 옮긴 JSON Lines가 있으면 그것을 본다."""
        p = self._path(series_id, part)
        if p.exists():
            return p
        legacy = self._path(series_id, part, ".jsonl")
        return legacy if series_id in self.parquet_series and legacy.exists() else None

    def _files(self, series_ids=None, parts: Optional[Iterable[tuple]] = None) -> list[Path]:
        if parts is not None:
            return [p for p in (self._existing(s, k) for s, k in sorted(set(parts))) if p is not None]
        sids = sorted(series_ids) if series_ids is not None else self.series_ids()
        out = []
        for s in sids:
            d = self.root / "pit_versions" / _safe(s, "series_id")
            stems = sorted({p.stem for p in d.glob("*.jsonl")} | {p.stem for p in d.glob("*.parquet")})
            out += [p for p in (self._existing(s, k) for k in stems) if p is not None]
        return out

    def _read(self, path: Path) -> Iterator[PITRecord]:
        if path.suffix == ".parquet":
            yield from read_parquet(path)
            return
        with path.open(encoding="utf-8") as f:
            for line in f:
                yield record_from_json(json.loads(line))

    def iter_records(self, series_ids: Optional[Iterable[str]] = None, *,
                     parts: Optional[Iterable[tuple]] = None) -> Iterator[PITRecord]:
        """series_ids의 모든 파일, 또는 parts=[(series_id, partition), …]에 해당하는 파일만."""
        for p in self._files(series_ids, parts):
            yield from self._read(p)

    def migrate_to_parquet(self, series_id: str) -> dict:
        """그 시리즈의 JSON Lines 파티션을 모두 Parquet로 옮긴다. 옮긴 뒤 다시 읽어 레코드가 같은지 확인하고 원래 파일을 지운다."""
        if series_id not in self.parquet_series:
            raise ValueError(f"{series_id}는 parquet_series가 아님")
        d = self.root / "pit_versions" / _safe(series_id, "series_id")
        moved = rows = 0
        for legacy in sorted(d.glob("*.jsonl")):
            recs = list(self._read(legacy))
            for cid in sorted({r.license_contract_id for r in recs}):
                require_placement(self.registry, cid, self.root, Purpose.PUBLIC_DISPLAY)
            target = legacy.with_suffix(".parquet")
            if target.exists():
                raise ValueError(f"{target}가 이미 있음 — 같은 파티션이 두 형식으로 있으면 옮기지 않는다")
            write_parquet(target, recs)
            if read_parquet(target) != recs:
                target.unlink()
                raise ValueError(f"{legacy}: Parquet로 옮긴 레코드가 원래와 다름 — 원래 파일을 남기고 멈춤")
            legacy.unlink()
            moved, rows = moved + 1, rows + len(recs)
        return {"files": moved, "records": rows}

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
        for cid in sorted({r.license_contract_id for r in new}):      # 공개 저장소에는 공개 표시가 허용된 값만
            require_placement(self.registry, cid, self.root, Purpose.PUBLIC_DISPLAY)
        by_part: dict[tuple, list[PITRecord]] = {}
        for r in new:
            by_part.setdefault((r.series_id, self.partition_of(r)), []).append(r)
        for (series_id, part), recs in sorted(by_part.items()):
            path = self._path(series_id, part)
            if series_id in self.parquet_series:
                old_file = self._existing(series_id, part)
                existing = list(self._read(old_file)) if old_file is not None else []
                write_parquet(path, existing + recs)          # 기존 순서 그대로 + 새 레코드
                if old_file is not None and old_file != path:
                    old_file.unlink()                         # JSON Lines에서 옮겨 왔으면 원래 파일을 지운다
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write("\n".join(json.dumps(record_to_json(r), ensure_ascii=False, sort_keys=True) for r in recs) + "\n")
        return AppendResult(len(new), unchanged, tuple(conflicts))
