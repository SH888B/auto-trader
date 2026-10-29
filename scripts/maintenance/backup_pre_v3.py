# -*- coding: utf-8 -*-
"""Create a rollback copy and hash inventory before the v3 workspace migration."""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKUP = ROOT / "archive" / "pre-v3-20261028"

COPY_PATHS = [
    "ARCHITECTURE.md", "ARCHITECTURE-V3.md", "ARCHITECTURE-V3-PROPOSAL.md",
    "WORKSPACE.md", "PRD-E002-老骚对照-v1.md", "PRD-E003-复盘框架v2-v1.md",
    "PRD-Judge-实验机制-v1.md", "PRD-Scout-v1.md", "pyproject.toml", "uv.lock",
    "selectors.yaml", "universe.yaml", "context", "contracts", "framework",
    "scripts", "workflows", "data/watchlist.yaml", "data/positions",
    "data/snapshots", "data/assets", "data/watchlist-w1.0-20261027.yaml",
]
SKIP_DIRS = {".venv", "archive", "temp", "__pycache__", ".git"}


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    if BACKUP.exists():
        raise SystemExit(f"Backup already exists: {BACKUP}")
    files_dir = BACKUP / "files"
    files_dir.mkdir(parents=True)
    for rel in COPY_PATHS:
        src = ROOT / rel
        if not src.exists():
            continue
        dst = files_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(src, dst)

    inventory = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        rel = path.relative_to(ROOT).as_posix()
        inventory.append({"path": rel, "bytes": path.stat().st_size, "sha256": digest(path)})
    payload = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "root": str(ROOT),
        "file_count": len(inventory),
        "files": inventory,
    }
    (BACKUP / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[backup] {BACKUP}")
    print(f"[inventory] {len(inventory)} files")


if __name__ == "__main__":
    main()
