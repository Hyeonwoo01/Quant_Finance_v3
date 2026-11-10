"""변경 불가 원본 저장소 (가이드 6-1, 6-2).

- 저장(STORAGE) 권한이 승인된 계약의 응답만 보관한다. 권한이 없으면 디스크에 쓰기 전에 막는다.
- 내용 주소 방식: 같은 응답은 SHA-256이 같으므로 한 번만 저장하고, 받을 때마다 수집 기록(index)만 한 줄 더한다.
- 이미 있는 파일은 덮어쓰지 않는다. 같은 해시 경로에 다른 내용이 있으면 오류 (변조·손상 감지).
- 요청 파라미터의 비밀 값(인증키 등)은 기록 전에 한 번 더 가린다.

배치:  <root>/raw/<source_id>/objects/<sha[:2]>/<sha>.gz
       <root>/raw/<source_id>/index/<YYYY-MM-DD>.jsonl   (수신 날짜 UTC 기준, 받은 순서대로)
"""
import gzip
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from collectors.base_client import HttpResponse, redact
from core.data_contracts import ContractRegistry, Purpose
from storage.visibility import require_placement


@dataclass(frozen=True)
class RawRef:
    source_id: str
    sha256: str
    relpath: str            # root 기준 경로
    is_new: bool            # 이번에 처음 저장한 내용인지


def sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _write_new(path: Path, data: bytes) -> None:
    """임시 파일에 쓴 뒤 이름을 바꿔, 중간에 끊겨도 반쯤 쓴 파일이 남지 않게 한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class RawStore:
    def __init__(self, root, registry: ContractRegistry):
        self.root, self.registry = Path(root), registry

    def _object_path(self, source_id: str, sha: str) -> Path:
        return self.root / "raw" / source_id / "objects" / sha[:2] / f"{sha}.gz"

    def put(self, *, contract_id: str, source_id: str, response: HttpResponse, request_id: Optional[str] = None) -> RawRef:
        self.registry.require(contract_id, Purpose.STORAGE)   # 권한 없으면 PermissionDenied, 아무것도 쓰지 않음
        require_placement(self.registry, contract_id, self.root, Purpose.RAW_REDISTRIBUTION)   # 공개 저장소에는 재배포 허용 원본만
        sha = sha256_hex(response.body)
        path = self._object_path(source_id, sha)
        is_new = not path.exists()
        if is_new:
            _write_new(path, gzip.compress(response.body, mtime=0))   # mtime=0: 같은 내용이면 같은 파일
        elif sha256_hex(gzip.decompress(path.read_bytes())) != sha:
            raise ValueError(f"{path}: 저장된 원본의 해시가 경로와 다름 — 손상 또는 변조")
        entry = {"sha256": sha, "contract_id": contract_id, "source_id": source_id, "request_id": request_id,
                 "url": response.url, "params": redact(response.params), "http_status": response.status,
                 "attempts": response.attempts, "requested_at_utc": response.requested_at_utc.isoformat(),
                 "received_at_utc": response.received_at_utc.isoformat(), "bytes": len(response.body),
                 "new_object": is_new}
        index = self.root / "raw" / source_id / "index" / f"{response.received_at_utc:%Y-%m-%d}.jsonl"
        index.parent.mkdir(parents=True, exist_ok=True)
        with index.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        return RawRef(source_id, sha, str(path.relative_to(self.root)), is_new)

    def get(self, source_id: str, sha: str) -> bytes:
        body = gzip.decompress(self._object_path(source_id, sha).read_bytes())
        if sha256_hex(body) != sha:
            raise ValueError(f"{source_id}/{sha}: 해시 불일치")
        return body

    def index_entries(self, source_id: str) -> Iterator[dict]:
        for p in sorted((self.root / "raw" / source_id / "index").glob("*.jsonl")):
            with p.open(encoding="utf-8") as f:
                for line in f:
                    yield json.loads(line)
