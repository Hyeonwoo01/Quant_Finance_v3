"""재현성 정보: 어떤 코드 버전으로 만든 결과인지."""
import os
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


def code_version() -> str:
    """GitHub Actions면 GITHUB_SHA, 아니면 git HEAD (작업 중 변경이 있으면 '-dirty'). 알 수 없으면 'unknown'."""
    sha = os.environ.get("GITHUB_SHA")
    if sha:
        return sha
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=_REPO, capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=_REPO,
                               capture_output=True, text=True, check=True).stdout.strip()
        return head + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
