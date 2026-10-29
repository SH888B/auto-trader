# -*- coding: utf-8 -*-
"""Read-only integrity check for a finalized Scout snapshot."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="校验 Scout snapshot.yaml 结构、日期与文件 hash")
    parser.add_argument("--date", required=True, help="交易日 YYYYMMDD")
    parser.add_argument("--allow-degraded", action="store_true", help="允许存在已记录的缺口")
    args = parser.parse_args()
    date_dash = f"{args.date[:4]}-{args.date[4:6]}-{args.date[6:]}"
    path = ROOT / "data" / "snapshots" / date_dash / "snapshot.yaml"
    if not path.exists():
        raise SystemExit(f"snapshot 缺失：{path}")
    snapshot = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    errors = []
    if snapshot.get("snapshot_version") != "s3.0":
        errors.append(f"unexpected snapshot_version: {snapshot.get('snapshot_version')}")
    if snapshot.get("trade_date") != date_dash:
        errors.append(f"trade_date 不匹配：{snapshot.get('trade_date')} != {date_dash}")
    for group in ("derived_files", "raw_inputs"):
        for name, item in (snapshot.get(group) or {}).items():
            artifact = ROOT / item.get("path", "")
            if not artifact.is_file():
                errors.append(f"{group}/{name}: 文件不存在 {artifact}")
            elif sha256(artifact) != item.get("sha256"):
                errors.append(f"{group}/{name}: hash 不匹配")
    if snapshot.get("status") == "degraded" and not args.allow_degraded:
        errors.extend(f"已记录缺口：{gap}" for gap in snapshot.get("gaps") or [])
    if errors:
        print("[snapshot invalid]")
        for error in errors:
            print(f"  - {error}")
        raise SystemExit(1)
    print(f"[snapshot OK] {path}")
    print(f"[derived] {len(snapshot.get('derived_files', {}))} 张表；status={snapshot.get('status')}")


if __name__ == "__main__":
    main()
