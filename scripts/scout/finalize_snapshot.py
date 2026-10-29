# -*- coding: utf-8 -*-
"""Finalize a Scout data snapshot without creating an Agent-facing digest.

Run after build_snapshot.py, load_to_warehouse.py and select_stocks.py:
    uv run python scripts/scout/finalize_snapshot.py --date 20261027
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def ref(path: Path) -> dict:
    return {"path": path.relative_to(ROOT).as_posix(), "sha256": sha256(path)}


def scripts_hash() -> str:
    h = hashlib.sha256()
    for path in sorted((ROOT / "scripts" / "scout").glob("*.py")):
        h.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        h.update(path.read_bytes())
    return h.hexdigest()


def payload_items(path: Path) -> list:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload.get("items"), list):
        return payload["items"]
    data = payload.get("data") or {}
    return data.get("items") or []


def csv_rows(path: Path) -> int:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return max(sum(1 for _ in csv.reader(f)) - 1, 0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scout：冻结数据快照清单（不生成 Digest）")
    parser.add_argument("--date", required=True, help="交易日 YYYYMMDD")
    args = parser.parse_args()
    if len(args.date) != 8 or not args.date.isdigit():
        raise SystemExit("date 必须为 YYYYMMDD")
    date_dash = f"{args.date[:4]}-{args.date[4:6]}-{args.date[6:]}"
    snap_dir = ROOT / "data" / "snapshots" / date_dash
    derived_dir = snap_dir / "derived"
    if not derived_dir.is_dir():
        raise SystemExit(f"缺少 derived 目录，请先运行 select_stocks.py：{derived_dir}")

    data_recipe_path = ROOT / "configs" / "data" / "data_recipe.yaml"
    data_recipe = load_yaml(data_recipe_path)
    selectors_path = ROOT / data_recipe.get("selectors", "configs/data/selectors.yaml")
    universe_path = ROOT / data_recipe.get("universe", "configs/data/universe.yaml")
    selectors = load_yaml(selectors_path)
    universe = load_yaml(universe_path)

    derived_files = {}
    derived_rows = {}
    for path in sorted(derived_dir.glob("*.csv")):
        rows = csv_rows(path)
        derived_rows[path.stem] = rows
        derived_files[path.stem] = {**ref(path), "rows": rows}

    gaps: list[str] = []
    expected = []
    for name in selectors.get("groups", {}):
        if name != "watchlist":
            expected.append(name)
    for name in ("ceiling", "carrier_lifecycle"):
        if selectors.get(name if name == "ceiling" else "lifecycle"):
            expected.append(name)
    for name in dict.fromkeys(expected):
        if name not in derived_files:
            gaps.append(f"derived/{name}.csv 缺失（配置要求生成）")

    raw_specs = [
        ("daily", f"daily_{args.date}.json", True),
        ("stk_limit", f"stk_limit_{args.date}.json", True),
        ("limit_list_d", f"limit_list_d_{args.date}_up.json", True),
        ("limit_list_d", f"limit_list_d_{args.date}_down.json", True),
        ("kpl_list", f"kpl_list_{args.date}_涨停.json", False),
        ("limit_cpt_list", f"limit_cpt_list_{args.date}.json", True),
    ]
    raw_inputs = {}
    for api, filename, required in raw_specs:
        path = ROOT / "data" / "raw" / api / filename
        key = f"{api}/{filename}"
        if not path.exists():
            if required or selectors.get("limit_source", {}).get("kpl_enrich", True):
                gaps.append(f"raw/{key} 缺失")
            continue
        rows = len(payload_items(path))
        raw_inputs[key] = {**ref(path), "rows": rows}
        if required and rows == 0:
            gaps.append(f"raw/{key} 为空响应")
        if api == "kpl_list" and rows == 0 and selectors.get("limit_source", {}).get("kpl_enrich", True):
            gaps.append(f"raw/{key} 为空响应（次日增强尚未到位）")

    stock_basic = ROOT / "data" / "raw" / "stock_basic"
    if not stock_basic.exists() or not any(stock_basic.glob("*.json")):
        gaps.append("stock_basic 缺失（股票名称/ST 过滤降级）")

    derived_code_version = f"select_stocks:{sha256(ROOT / 'scripts/scout/select_stocks.py')[:16]}"
    generated_day = datetime.now().astimezone().date().isoformat()
    manifest = {
        "snapshot_version": "s3.0",
        "trade_date": date_dash,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data_as_of": date_dash,
        "derived_code_version": derived_code_version,
        "backfilled": generated_day != date_dash,
        "status": "degraded" if gaps else "complete",
        "data_recipe": ref(data_recipe_path),
        "selectors": ref(selectors_path),
        "universe": ref(universe_path),
        "selectors_version": selectors.get("version", "?"),
        "universe_version": universe.get("version", "?"),
        "scripts_sha256": scripts_hash(),
        "derived_rows": derived_rows,
        "derived_files": derived_files,
        "raw_inputs": raw_inputs,
        "availability": {
            "decision_date": date_dash,
            "granularity": "trade_date",
            "note": "文件 hash 与生成时间已锁定；as_of 目前为交易日粒度，精确到秒的可用时间隔离仍需数据级 available_at。",
            "late_enrichments": ["kpl_list 可能在交易日之后更新；历史回放须确认其在实际决策前已可用"],
        },
        "gaps": list(dict.fromkeys(gaps)),
    }
    output = snap_dir / "snapshot.yaml"
    output.write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"[snapshot] {output} status={manifest['status']}")
    print(f"[derived] {len(derived_files)} 张表，{sum(derived_rows.values())} 行")
    for gap in manifest["gaps"]:
        print(f"  [gap] {gap}")


if __name__ == "__main__":
    main()
