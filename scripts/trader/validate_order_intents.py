# -*- coding: utf-8 -*-
"""Schema and semantic validation for Trader order_intents.json (v1.1)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from jsonschema import Draft7Validator

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))
from hashutil import ROOT, sha256, workspace_path  # noqa: E402


def load_positions(order_path: Path, data: dict, errors: list[str]) -> dict[str, float]:
    """v1.1：position_state_ref 存在时加载 T 日收盘持仓 {ts_code: open_pct}。"""
    ref = data.get("position_state_ref") or {}
    artifact = workspace_path(ref.get("path", ""))
    if not artifact.is_file():
        errors.append(f"position_state_ref: 文件不存在 {artifact}")
        return {}
    if sha256(artifact) != ref.get("sha256"):
        errors.append(f"position_state_ref: hash 不匹配 {artifact}")
        return {}
    state = yaml.safe_load(artifact.read_text(encoding="utf-8")) or {}
    holdings: dict[str, float] = {}
    for pos in state.get("positions", []):
        if pos.get("status") == "open":
            holdings[pos["ts_code"]] = holdings.get(pos["ts_code"], 0.0) + float(pos.get("pct", 0))
    return holdings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("instance")
    args = parser.parse_args()
    path = (ROOT / args.instance).resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    schema = json.loads((ROOT / "contracts/order_intents.schema.json").read_text(encoding="utf-8"))
    errors = [f"{'.'.join(map(str, e.path))}: {e.message}" for e in Draft7Validator(schema).iter_errors(data)]

    for key in ("judge_review_ref", "account_state_ref", "query_manifest_ref"):
        ref = data.get(key) or {}
        artifact = ROOT / ref.get("path", "")
        if not artifact.is_file():
            errors.append(f"{key}: 文件不存在 {artifact}")
        elif sha256(artifact) != ref.get("sha256"):
            errors.append(f"{key}: hash 不匹配 {artifact}")

    holdings = load_positions(path, data, errors) if data.get("position_state_ref") else {}

    total_buy = 0.0
    for i, action in enumerate(data.get("actions", [])):
        kind = action.get("action")
        price, stop, take = action.get("limit_price"), action.get("stop_loss"), action.get("take_profit")
        pct = float(action.get("position_pct", 0))
        if kind == "buy":
            total_buy += pct
            if not all(isinstance(v, (int, float)) for v in (price, stop, take)):
                errors.append(f"actions[{i}]: buy 必须有挂价、止损、止盈")
            elif not stop < price < take:
                errors.append(f"actions[{i}]: 必须 stop_loss < limit_price < take_profit")
        if kind in ("sell", "hold"):
            if not holdings:
                errors.append(f"actions[{i}]: {kind} 单必须提供可校验的 position_state_ref 持仓")
            elif action.get("ts_code") not in holdings:
                errors.append(f"actions[{i}]: {kind} 标的 {action.get('ts_code')} 不在持仓中 {sorted(holdings)}")
            elif kind == "hold" and pct > holdings[action["ts_code"]] + 1e-9:
                errors.append(f"actions[{i}]: hold 仓位 {pct} 超过持仓 {holdings[action['ts_code']]}")
            # sell 的 position_pct 语义为"卖出该持仓的比例"（1.0=清仓），不与账户占比比较
        if kind == "cancel" and action.get("order_type") != "none":
            errors.append(f"actions[{i}]: cancel 的 order_type 必须为 none")
    if total_buy > 1.0:
        errors.append(f"buy 仓位合计 {total_buy:.2f} > 1.0")
    if holdings and total_buy + sum(holdings.values()) > 1.0 + 1e-9:
        errors.append(f"买入后总仓位 {total_buy + sum(holdings.values()):.2f} > 1.0")
    if not data.get("actions") and not data.get("no_trade_reason"):
        errors.append("空仓时必须填写 no_trade_reason")

    if errors:
        print("[校验失败]")
        for error in errors:
            print(f"  - {error}")
        raise SystemExit(1)
    print(f"[OK] {path}（{len(data.get('actions', []))} 个订单意图）"
          + (f"，持仓校验通过 {len(holdings)} 只" if holdings else ""))


if __name__ == "__main__":
    main()
