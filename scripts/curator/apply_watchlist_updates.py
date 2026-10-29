# -*- coding: utf-8 -*-
"""Apply Judge-owned watchlist_updates to an explicit target and freeze a dated snapshot."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import yaml
from jsonschema import Draft7Validator

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review", required=True)
    parser.add_argument("--target", required=True, help="Judge watchlist YAML，不允许 user watchlist")
    parser.add_argument("--snapshot", help="历史快照路径；默认 target 同级 history/<as_of>.yaml")
    args = parser.parse_args()
    review_path = (ROOT / args.review).resolve()
    target = (ROOT / args.target).resolve()
    if not target.is_relative_to(ROOT):
        raise SystemExit("target 必须位于 workspace 内")
    if "user" in target.parts:
        raise SystemExit("user watchlist 只能由用户修改")
    review = json.loads(review_path.read_text(encoding="utf-8"))
    schema = json.loads((ROOT / "contracts/review.schema.json").read_text(encoding="utf-8"))
    errors = list(Draft7Validator(schema).iter_errors(review))
    if errors:
        raise SystemExit(f"review 契约校验失败：{errors[0].message}")
    state = yaml.safe_load(target.read_text(encoding="utf-8")) or {"stocks": []}
    stocks = state.setdefault("stocks", [])

    for update in review.get("watchlist_updates", []):
        code = update["ts_code"]
        matches = [s for s in stocks if s.get("ts_code") == code]
        operation = update["operation"]
        if operation == "add" and not matches:
            stocks.append({
                "ts_code": code, "theme_id": update.get("theme_id"), "role": update.get("role"),
                "level": update.get("level", "npc"), "status": "关注", "reason": update["reason"],
                "label_source": "judge", "added_date": review["as_of"], "available_at": review["as_of"],
            })
        elif matches:
            item = matches[0]
            if operation == "remove":
                item["status"] = "移出"
            elif operation == "upgrade":
                item["level"] = update.get("level", item.get("level"))
            elif operation == "downgrade":
                item["level"] = update.get("level", item.get("level"))
            if update.get("role"):
                item["role"] = update["role"]
            item["reason"] = update["reason"]
            item["available_at"] = review["as_of"]

    state["updated"] = review["as_of"]
    state["label_source"] = "judge"
    rendered = yaml.safe_dump(state, allow_unicode=True, sort_keys=False)
    snapshot = Path(args.snapshot).resolve() if args.snapshot else target.parent / "history" / f"{review['as_of']}.yaml"
    if not snapshot.is_relative_to(ROOT):
        raise SystemExit("snapshot 必须位于 workspace 内")
    if snapshot.exists() and snapshot.read_text(encoding="utf-8") != rendered:
        raise SystemExit(f"历史快照已存在且内容不同，拒绝覆盖：{snapshot}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    if not snapshot.exists():
        snapshot.write_text(rendered, encoding="utf-8")
    print(f"[watchlist] updates={len(review.get('watchlist_updates', []))} target={target}")
    print(f"[snapshot] {snapshot}")


if __name__ == "__main__":
    main()
