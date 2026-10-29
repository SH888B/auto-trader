# -*- coding: utf-8 -*-
"""Render a deterministic, point-in-time Agent digest from a Scout snapshot.

Example:
    uv run python scripts/curator/render_digest.py --date 20261027 \
        --view configs/digest/judge_default.yaml --run-id live-20261027

Scout owns the data. Curator owns only this view and its input manifest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
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


DICT_PATH = ROOT / "configs/digest/column_dictionary.yaml"


def load_dictionary() -> tuple[dict, set[str]]:
    d = load_yaml(DICT_PATH)
    return d.get("columns", {}), set(d.get("exclude_columns", []))


def fmt_cell(value, spec: dict | None) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, (int, float)) and spec:
        v = float(value)
        div = {"yi5": 1e5, "yi8": 1e8}.get(str(spec.get("fmt")), 1)
        ndec = {"price2": 2, "pct2": 2, "yi5": 2, "yi8": 2, "int0": 0}.get(str(spec.get("fmt")), 2)
        return f"{v / div:.{ndec}f}"
    return fmt(value)


def markdown_table(df: pd.DataFrame, columns: list[str], limit: int,
                   dictionary: dict, exclude: set[str]) -> str:
    if df.empty:
        return "（无数据）"
    cols = [c for c in columns if c in df.columns] if columns else list(df.columns)
    cols = [c for c in cols if c not in exclude]
    if not cols:
        return "（配置列均不存在）"
    view = df.loc[:, cols].head(limit)
    header = [dictionary.get(c, {}).get("zh", c) for c in cols]
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join(["---"] * len(cols)) + "|",
    ]
    for _, row in view.iterrows():
        lines.append("| " + " | ".join(fmt_cell(row[c], dictionary.get(c)) for c in cols) + " |")
    return "\n".join(lines)


def ref(path: Path) -> dict:
    return {"path": path.relative_to(ROOT).as_posix(), "sha256": sha256(path)}


def fmt(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value).replace("|", "\\|").replace("\n", " ")


def main() -> None:
    parser = argparse.ArgumentParser(description="Curator：从标准快照渲染 Agent Digest")
    parser.add_argument("--date", required=True, help="决策时点 YYYYMMDD")
    parser.add_argument("--view", default="configs/digest/judge_default.yaml")
    parser.add_argument("--data", default="configs/data/data_recipe.yaml")
    parser.add_argument("--output", help="Digest 输出路径；默认 runs/live/<date>/judge/digest.md")
    parser.add_argument("--run-id", help="写入 input_manifest 的 Run ID")
    parser.add_argument("--module", default="judge", choices=["judge", "trader"])
    args = parser.parse_args()

    date_dash = f"{args.date[:4]}-{args.date[4:6]}-{args.date[6:]}"
    view_path = (ROOT / args.view).resolve()
    data_path = (ROOT / args.data).resolve()
    view = load_yaml(view_path)
    data_recipe = load_yaml(data_path)
    snap_dir = ROOT / "data" / "snapshots" / date_dash
    snapshot_path = snap_dir / "snapshot.yaml"
    if not snapshot_path.exists():
        raise SystemExit(f"数据快照不存在：{snapshot_path}")
    snapshot = load_yaml(snapshot_path)
    dictionary, exclude = load_dictionary()

    # P0-1 硬校验：时点隔离从约定升级为机制
    data_as_of = str(snapshot.get("data_as_of") or "")
    if not data_as_of:
        raise SystemExit(f"snapshot 缺少 data_as_of（P0-1 硬校验拒绝）：{snapshot_path}")
    if data_as_of < date_dash:
        raise SystemExit(f"snapshot 数据未覆盖请求日期：data_as_of={data_as_of} < as_of={date_dash}")
    derived_ver = snapshot.get("derived_code_version")
    if not derived_ver:
        raise SystemExit(f"snapshot 缺少 derived_code_version（P0-1 硬校验拒绝）：{snapshot_path}")

    output = Path(args.output).resolve() if args.output else (
        ROOT / "runs" / "live" / date_dash / args.module / "digest.md")
    output.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        f"# {args.module.title()} Digest —— {date_dash}",
        "",
        f"> as_of: {date_dash} ｜ view: {view.get('version', '?')} ｜ "
        f"data: {data_recipe.get('version', '?')} ｜ 仅含决策时点允许数据",
        "",
    ]
    inputs = [ref(snapshot_path), ref(view_path), ref(data_path), ref(DICT_PATH)]
    gaps = list(snapshot.get("gaps") or [])
    meta: dict[str, str] = {}

    def note_meta(df: pd.DataFrame) -> None:
        for col in ("selectors_version", "trade_date"):
            if col in df.columns:
                values = {str(v) for v in df[col].dropna().unique()}
                if len(values) == 1:
                    meta[col] = values.pop()

    for section in view.get("sections", []):
        title = section["title"]
        lines.extend([f"## {title}", ""])
        if section.get("note"):
            for note_line in str(section["note"]).strip().splitlines():
                lines.append(f"> {note_line.strip()}")
            lines.append("")
        if section.get("type") == "breadth":
            path = ROOT / "data" / "curated" / date_dash / "breadth.csv"
            if path.exists():
                df = pd.read_csv(path, encoding="utf-8-sig")
                note_meta(df)
                lines.append(markdown_table(df, section.get("columns", []), int(section.get("display_n", 100)), dictionary, exclude))
                inputs.append(ref(path))
            else:
                lines.append("（breadth 缺失）")
                gaps.append(f"{title}: breadth.csv 缺失")
            lines.append("")
            continue

        table = section["table"]
        path = snap_dir / "derived" / f"{table}.csv"
        if not path.exists():
            lines.append(f"（{table} 缺失）")
            lines.append("")
            gaps.append(f"{title}: derived/{table}.csv 缺失")
            continue
        df = pd.read_csv(path, encoding="utf-8-sig")
        note_meta(df)
        display_n = int(section.get("display_n", 20))
        lines.append(f"标准表共 {len(df)} 行；本视图展开前 {min(display_n, len(df))} 行。")
        lines.append("")
        lines.append(markdown_table(df, section.get("columns", []), display_n, dictionary, exclude))
        lines.append("")
        inputs.append(ref(path))

    if meta:
        lines[2] += " ｜ " + " ｜ ".join(f"{k}: {v}" for k, v in sorted(meta.items()))
    lines[2] += f" ｜ derived_code_version: {derived_ver}"

    lines.extend(["## 数据缺口", ""])
    unique_gaps = list(dict.fromkeys(str(g) for g in gaps if str(g) != "无"))
    lines.extend([f"- {g}" for g in unique_gaps] or ["- 无"])
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")

    manifest = {
        "manifest_version": "im1.0",
        "run_id": args.run_id or f"live-{args.date}-{args.module}",
        "module": args.module,
        "as_of": date_dash,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data_snapshot": ref(snapshot_path),
        "data_recipe": ref(data_path),
        "view_recipe": ref(view_path),
        "digest": ref(output),
        "inputs": inputs,
        "queries": [],
        "gaps": unique_gaps,
    }
    manifest_path = output.parent / "input_manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"[digest] {output}")
    print(f"[manifest] {manifest_path}")


if __name__ == "__main__":
    main()
