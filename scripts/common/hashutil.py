# -*- coding: utf-8 -*-
"""Workspace 文件哈希与 artifact 引用工具（R1：各脚本共用一份实现）。"""
from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def workspace_path(value: str | Path) -> Path:
    path = Path(value)
    return (ROOT / path).resolve() if not path.is_absolute() else path.resolve()


def artifact_ref(path: Path) -> dict:
    """{path: workspace 相对路径, sha256}；path 必须在 workspace 内。"""
    path = path.resolve()
    if not path.is_relative_to(ROOT):
        raise SystemExit(f"artifact 必须位于 workspace 内：{path}")
    return {"path": path.relative_to(ROOT).as_posix(), "sha256": sha256(path)}
