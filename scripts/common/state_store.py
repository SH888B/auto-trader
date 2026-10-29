# -*- coding: utf-8 -*-
"""按天演化的状态存储通用件（R2）。

语义：
- `<target>`（current.yaml / xxx.yaml）是"最新收盘状态"缓存视图；
- `history/<date>.yaml` 是只追加的每日快照，代表"该日收盘后"状态；
- as_of 查询永远回退到 history，不读未冻结的 current；
- history 快照已存在且内容不同 → 拒绝覆盖（防篡改/防重放错乱）。

三个记忆（judge watchlist / user watchlist / portfolio）共用本模块，
各 apply 脚本只写"如何把 updates 迁移到 state"，读写与快照在这里。
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def resolve(path: str | Path) -> Path:
    p = Path(path)
    p = (ROOT / p).resolve() if not p.is_absolute() else p.resolve()
    if not p.is_relative_to(ROOT):
        raise SystemExit(f"状态文件必须位于 workspace 内：{p}")
    return p


def load(path: str | Path) -> dict:
    p = resolve(path)
    if not p.is_file():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def as_of_path(target: str | Path, as_of: str, history_dir: str | Path | None = None) -> Path | None:
    """返回 as_of 收盘时的状态文件路径（history 回退查询）；无则 None。"""
    target = resolve(target)
    hdir = resolve(history_dir) if history_dir else target.parent / "history"
    if not hdir.is_dir():
        return None
    candidates = sorted(p for p in hdir.glob("*.yaml") if p.stem <= as_of)
    return candidates[-1] if candidates else None


def load_as_of(target: str | Path, as_of: str, history_dir: str | Path | None = None) -> dict:
    p = as_of_path(target, as_of, history_dir)
    return load(p) if p else {}


def save(target: str | Path, state: dict, snapshot_date: str | None = None,
         history_dir: str | Path | None = None) -> Path | None:
    """写 current + 可选每日快照；快照冲突拒绝覆盖。返回快照路径。"""
    target = resolve(target)
    rendered = yaml.safe_dump(state, allow_unicode=True, sort_keys=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    if snapshot_date is None:
        return None
    hdir = resolve(history_dir) if history_dir else target.parent / "history"
    snapshot = hdir / f"{snapshot_date}.yaml"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    if snapshot.exists() and snapshot.read_text(encoding="utf-8") != rendered:
        raise SystemExit(f"历史快照已存在且内容不同，拒绝覆盖：{snapshot}")
    if not snapshot.exists():
        snapshot.write_text(rendered, encoding="utf-8")
    return snapshot
