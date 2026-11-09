"""수집 배치 기록 (가이드 6-2).

실행마다 무엇을 어떤 권한으로 언제 받았고, 몇 건이 새로 들어갔는지·무엇이 이상했는지를 남긴다. 실패한 실행도 남긴다.
배치: <root>/manifests/<source_id>/<시작 날짜 UTC YYYY-MM-DD>/<run_id>.json
"""
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.data_contracts import ApprovalStatus, DataSourceContract

SCHEMA_VERSION = "manifest-1"


@dataclass
class BatchManifest:
    run_id: str
    source_id: str
    contract_id: str
    code_version: str
    started_at_utc: datetime
    target: dict = field(default_factory=dict)              # 무엇을 받으려 했는지 (예: 접수일 범위)
    requests: list = field(default_factory=list)            # 요청별 기록 (가린 파라미터, 상태, 원본 해시, 건수)
    counts: dict = field(default_factory=dict)              # rows, duplicates, missing, parse_errors, added, unchanged …
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    permission: dict = field(default_factory=dict)          # 계약 상태·출처 표시 조건
    finished_at_utc: Optional[datetime] = None
    status: str = "running"                                 # running → ok / partial / failed
    schema_version: str = SCHEMA_VERSION

    @classmethod
    def start(cls, *, run_id: str, contract: DataSourceContract, code_version: str, target: dict,
              now: Optional[datetime] = None) -> "BatchManifest":
        return cls(run_id=run_id, source_id=contract.source_id, contract_id=contract.contract_id,
                   code_version=code_version, started_at_utc=now or datetime.now(timezone.utc), target=dict(target),
                   permission={"approval_status": ApprovalStatus(contract.approval_status).value,
                               "attribution_required": contract.attribution_required,
                               "license_url": contract.license_url,
                               "permission_reference": contract.permission_reference})

    def count(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def finish(self, now: Optional[datetime] = None) -> None:
        """오류가 없으면 ok, 오류가 있어도 일부를 받았으면 partial, 아무것도 못 받았으면 failed."""
        self.finished_at_utc = now or datetime.now(timezone.utc)
        got_something = self.counts.get("added", 0) + self.counts.get("unchanged", 0) > 0
        self.status = "ok" if not self.errors else "partial" if got_something else "failed"

    def to_json(self) -> dict:
        d = asdict(self)
        for k in ("started_at_utc", "finished_at_utc"):
            d[k] = d[k].isoformat() if d[k] else None
        return d

    def write(self, root) -> Path:
        path = Path(root) / "manifests" / self.source_id / f"{self.started_at_utc:%Y-%m-%d}" / f"{self.run_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path
