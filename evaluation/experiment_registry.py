"""실험 기록 (가이드 7-9, 7-12). 실험은 지우지 않고 추가만 한다 — 다중검정 보정의 입력이다.

- 저장: 비공개 저장소 experiments/<YYYY-MM>.jsonl (지표 외에 데이터 스냅샷·폴드·파라미터를 함께 둔다)
- 사전 등록: Vault 평가 전에 후보·파라미터·채택 기준을 experiments/preregistered.jsonl에 먼저 기록한다.
  같은 후보를 고쳐 다시 Vault에 넣지 않는다.
"""
import hashlib
import json
import platform
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.data_contracts import PermissionDenied
from storage.visibility import is_private_store


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def env_lock_hash(path: str = "requirements.txt") -> str:
    p = Path(path)
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.exists() else "unknown"


def data_snapshot_id(data_dir, series=("krx_openapi_daily", "krx_openapi_base")) -> str:
    """데이터 스냅샷: 시리즈별 파일 이름·크기 목록의 해시 (내용을 다 읽지 않고 바뀌었는지 알아본다)."""
    h = hashlib.sha256()
    for s in series:
        d = Path(data_dir) / "pit_versions" / s
        for p in sorted(d.glob("*.parquet")) if d.exists() else []:
            h.update(f"{s}/{p.name}:{p.stat().st_size}\n".encode())
    return h.hexdigest()[:16]


def list_hash(items) -> str:
    return hashlib.sha256("\n".join(items).encode()).hexdigest()[:16]


class ExperimentRegistry:
    def __init__(self, root):
        self.root = Path(root)

    def _dir(self) -> Path:
        if not is_private_store(self.root):
            raise PermissionDenied(f"{self.root}: 비공개 저장소가 아님 — 실험 기록에는 비공개 데이터 스냅샷 정보가 들어간다")
        d = self.root / "experiments"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def record(self, rec: dict, now: Optional[datetime] = None) -> dict:
        now = now or datetime.now(timezone.utc)
        rec = {"experiment_id": f"{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}", "created_at_utc": now.isoformat(),
               "git_commit": git_commit(), "environment_lock_hash": env_lock_hash(), "python": platform.python_version(),
               **rec}
        with (self._dir() / f"{now:%Y-%m}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        return rec

    def all(self) -> list:
        d = self.root / "experiments"
        out = []
        for p in sorted(d.glob("20*.jsonl")) if d.exists() else []:
            out += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        return out

    def count_trials(self, target_definition_id: str) -> int:
        """같은 타깃으로 몇 번 시험했는지 (다중검정 보정·DSR의 입력)."""
        return sum(len(r.get("models", [])) for r in self.all() if r.get("target_definition_id") == target_definition_id)
