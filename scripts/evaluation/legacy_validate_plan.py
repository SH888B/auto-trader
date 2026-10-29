# -*- coding: utf-8 -*-
"""plan.json 校验器（PRD-Judge 实验机制 §4.2）。

用法：
    uv run python scripts/judge/validate_plan.py <plan.json 路径> [--date 20261026]

两层校验：
1. 结构校验：contracts/plan.schema.json（字段/类型/枚举）；
2. 语义校验（PRD §4.2 规则 1-4 + E005 j0.2 增补）：
   - buy：stop_loss < price < take_profit，且三价与当日收盘偏离 ≤ ±20%；
   - hold（v3 持仓改单义务）：必须给 stop_loss/take_profit 数值且 stop_loss < take_profit；
   - avoid/sell：price 可为 0，stop_loss/take_profit 必须为 null；
   - 持仓覆盖：data/positions/current.yaml 里的每个持仓必须出现在 actions（hold/buy/sell）中；
   - ts_code 必须出现在当日 digest 的任一 derived CSV 或 watchlist.yaml（防幻觉）；
   - position_pct 合计 ≤ 0.5；空仓日（无 buy）合法。
校验失败退出码 1 并逐条打印原因；通过打印 [OK]。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scout"))
from tushare_client import ROOT  # noqa: E402

SCHEMA_PATH = ROOT / "contracts" / "plan.schema.json"


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def structural_check(plan: dict) -> list[str]:
    """schema 校验（不依赖 jsonschema 库，手写必填/类型/枚举/范围，错误信息中文）。"""
    errs: list[str] = []
    schema = load_schema()
    props = schema["properties"]

    for key in schema["required"]:
        if key not in plan:
            errs.append(f"缺必填字段: {key}")
    if errs:
        return errs

    if not isinstance(plan["date"], str) or len(plan["date"]) != 10:
        errs.append("date 须为 YYYY-MM-DD")
    if not (isinstance(plan["confidence"], (int, float)) and 0 <= plan["confidence"] <= 1):
        errs.append("confidence 须在 [0,1]")
    if not isinstance(plan["actions"], list):
        errs.append("actions 须为数组")
        return errs

    item_req = props["actions"]["items"]["required"]
    enums = props["actions"]["items"]["properties"]["action"]["enum"]
    tags = props["actions"]["items"]["properties"]["thesis_tag"]["enum"]
    code_pat = props["actions"]["items"]["properties"]["ts_code"]["pattern"]
    import re
    for i, a in enumerate(plan["actions"]):
        pre = f"actions[{i}]"
        for key in item_req:
            if key not in a:
                errs.append(f"{pre} 缺字段: {key}")
        if errs and any(e.startswith(pre) for e in errs):
            continue
        if not re.match(code_pat, str(a["ts_code"])):
            errs.append(f"{pre}.ts_code 格式非法: {a['ts_code']}")
        if a["action"] not in enums:
            errs.append(f"{pre}.action 非法: {a['action']}")
        if a["thesis_tag"] not in tags:
            errs.append(f"{pre}.thesis_tag 非法: {a['thesis_tag']}")
        if not isinstance(a["position_pct"], (int, float)) or not (0 <= a["position_pct"] <= 0.5):
            errs.append(f"{pre}.position_pct 须在 [0,0.5]")
        if not isinstance(a["price"], (int, float)) or a["price"] < 0:
            errs.append(f"{pre}.price 须 ≥ 0")
    return errs


def semantic_check(plan: dict, trade_date: str) -> list[str]:
    """语义校验：三价关系、偏离度、幻觉票、仓位合计。"""
    errs: list[str] = []

    # 当日 universe：digest derived 全部 CSV + watchlist
    date_dash = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
    der_dir = ROOT / "data" / "snapshots" / date_dash / "derived"
    valid_codes: set[str] = set()
    if der_dir.exists():
        import csv as _csv
        for csv_path in der_dir.glob("*.csv"):
            with open(csv_path, encoding="utf-8-sig") as f:
                for row in _csv.DictReader(f):
                    if row.get("ts_code"):
                        valid_codes.add(row["ts_code"])
    wl = ROOT / "state" / "watchlist" / "judge" / "current.yaml"
    if wl.exists():
        for s in (yaml.safe_load(wl.read_text(encoding="utf-8")) or {}).get("stocks", []):
            valid_codes.add(s["ts_code"])

    # 当日收盘价（偏离度用）
    import duckdb
    db = ROOT / "data" / "warehouse.duckdb"
    close_map: dict[str, float] = {}
    if db.exists():
        con = duckdb.connect(str(db), read_only=True)
        rows = con.execute(
            "SELECT ts_code, close FROM fact_daily_bar WHERE trade_date = ?", [date_dash]).fetchall()
        close_map = {c: cl for c, cl in rows}
        con.close()

    total_pct = 0.0
    for i, a in enumerate(plan["actions"]):
        pre = f"actions[{i}] {a.get('ts_code')}"
        if a["ts_code"] not in valid_codes:
            errs.append(f"{pre} 不在当日 digest/watchlist（幻觉票？）")
        act = a["action"]
        sl, tp, price = a["stop_loss"], a["take_profit"], a["price"]
        if act in ("avoid", "sell"):
            if sl is not None or tp is not None:
                errs.append(f"{pre} {act} 的 stop_loss/take_profit 须为 null")
        elif act == "hold":
            # v3 持仓改单义务：hold 必须带最新 stop_loss/take_profit
            if not (isinstance(sl, (int, float)) and isinstance(tp, (int, float))):
                errs.append(f"{pre} hold（持仓改单）必须给 stop_loss/take_profit 数值（j0.2）")
            elif not sl < tp:
                errs.append(f"{pre} hold 改单须 stop_loss({sl}) < take_profit({tp})")
        elif act == "buy":
            total_pct += a["position_pct"]
            if not (isinstance(sl, (int, float)) and isinstance(tp, (int, float))):
                errs.append(f"{pre} buy 必须给 stop_loss/take_profit 数值")
            elif not (sl < price < tp):
                errs.append(f"{pre} 三价关系须 stop_loss({sl}) < price({price}) < take_profit({tp})")
            close = close_map.get(a["ts_code"])
            if close and price > 0:
                dev = abs(price - close) / close
                if dev > 0.2:
                    errs.append(f"{pre} price 偏离当日收盘 {dev:.0%} > 20%（笔误？）")

    # v3 持仓覆盖：positions 里每个持仓必须出现在 actions（改单义务）
    pos_path = ROOT / "state" / "portfolio" / "current.yaml"
    if pos_path.exists():
        held = [p["ts_code"] for p in
                (yaml.safe_load(pos_path.read_text(encoding="utf-8")) or {}).get("positions", [])
                if isinstance(p, dict) and p.get("ts_code")]
        planned = {a["ts_code"] for a in plan["actions"]}
        for code in held:
            if code not in planned:
                errs.append(f"持仓 {code} 未出现在 plan（改单义务，j0.2）")

    if total_pct > 0.5:
        errs.append(f"buy 仓位合计 {total_pct:.2f} > 0.5")
    if not any(a["action"] == "buy" for a in plan["actions"]) \
            and not any(k in plan.get("market_view", "") for k in ("不开仓", "空仓")):
        errs.append("无 buy 的 plan 须在 market_view 写明'不开仓/空仓'理由")
    return errs


def main() -> None:
    parser = argparse.ArgumentParser(description="plan.json 校验器")
    parser.add_argument("plan", help="plan.json 路径")
    parser.add_argument("--date", required=True, help="判断日 YYYYMMDD（对应 plan.date 的前一交易日）")
    args = parser.parse_args()

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    errs = structural_check(plan) + semantic_check(plan, args.date)
    if errs:
        print("[校验失败]")
        for e in errs:
            print(f"  - {e}")
        sys.exit(1)
    print(f"[OK] {args.plan}（{len(plan['actions'])} 个动作）")


if __name__ == "__main__":
    main()
