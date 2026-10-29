# -*- coding: utf-8 -*-
"""Apply daily hypothetical fills to a portfolio state and freeze a dated snapshot.

多日持仓语义（与 score_v3 的单日回放刻意分离，Evaluator 重构时统一）：
- buy 成交 → 建立持仓（记录 cost/stop/take），跨日存续；
- 每个持仓在后续交易日按日线先检止损（含跳空）后检止盈，触发即退出；
- sell 意向按限价成交规则退出对应持仓；未成交的当日单过期；
- cash_pct = 1 - Σ(open pct)（暂不计盈亏漂移，见 DESIGN-P0-fixes §3）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb
import yaml
from jsonschema import Draft7Validator

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))
from hashutil import ROOT, sha256, workspace_path  # noqa: E402
import state_store  # noqa: E402

SCHEMA = json.loads((ROOT / "contracts/portfolio_state.schema.json").read_text(encoding="utf-8"))


def get_bars(con: duckdb.DuckDBPyConnection, day: str, codes: list[str]) -> dict[str, dict]:
    if not codes:
        return {}
    placeholders = ",".join("?" for _ in codes)
    rows = con.execute(
        f"SELECT ts_code, open, high, low, close FROM fact_daily_bar "
        f"WHERE trade_date = ? AND ts_code IN ({placeholders})", [day, *codes]).fetchall()
    return {r[0]: {"open": float(r[1]), "high": float(r[2]), "low": float(r[3]), "close": float(r[4])}
            for r in rows}


def buy_fill(action: dict, bar: dict | None) -> tuple[float | None, str]:
    if bar is None:
        return None, "pending_market_data"
    price = float(action["limit_price"])
    if bar["open"] <= price:
        return bar["open"], "fill_at_open"
    if bar["low"] <= price <= bar["high"]:
        return price, "fill_at_limit"
    return None, "unfilled_expired"


def sell_fill(action: dict, bar: dict | None) -> tuple[float | None, str]:
    if bar is None:
        return None, "pending_market_data"
    price = float(action["limit_price"])
    if bar["open"] >= price:
        return bar["open"], "fill_at_open"
    if bar["low"] <= price <= bar["high"]:
        return price, "fill_at_limit"
    return None, "unfilled_expired"


def close_position(pos: dict, exit_price: float, day: str, reason: str) -> None:
    pos["status"] = "closed"
    pos["exit_price"] = round(exit_price, 4)
    pos["exit_date"] = day
    pos["exit_reason"] = reason
    pos["realized_pnl_pct"] = round((exit_price - pos["cost_price"]) / pos["cost_price"] * 100, 4)
    pos["last_price"] = round(exit_price, 4)


def main() -> None:
    parser = argparse.ArgumentParser(description="按日迁移组合持仓并冻结快照")
    parser.add_argument("--order-intents", required=True)
    parser.add_argument("--target", required=True, help="portfolio 状态 YAML")
    parser.add_argument("--db", default="data/warehouse.duckdb")
    parser.add_argument("--history-dir", help="默认 current.yaml → 同级 history/；其他 → <stem>_history/")
    parser.add_argument("--snapshot-date", help="默认取 order-intents 的 valid_for")
    args = parser.parse_args()

    intents_path = workspace_path(args.order_intents)
    target = state_store.resolve(args.target)
    history_dir = (state_store.resolve(args.history_dir) if args.history_dir else
                   target.parent / "history" if target.name == "current.yaml" else
                   target.parent / f"{target.stem}_history")
    intents = json.loads(intents_path.read_text(encoding="utf-8"))
    valid_for = args.snapshot_date or intents["valid_for"]

    state = state_store.load(target) or {"version": "portfolio1.0", "cash_pct": 1.0,
                                         "positions": [], "open_orders": []}
    errors = list(Draft7Validator(SCHEMA).iter_errors(state))
    if errors:
        raise SystemExit(f"portfolio 状态校验失败：{errors[0].message}")

    con = duckdb.connect(str(workspace_path(args.db)), read_only=True)
    try:
        # 1) 存量持仓：今日 stop/take 检查与盯市
        open_pos = [p for p in state["positions"] if p.get("status") == "open"]
        bars = get_bars(con, valid_for, sorted({p["ts_code"] for p in open_pos}))
        exits = []
        for pos in open_pos:
            bar = bars.get(pos["ts_code"])
            if bar is None:
                continue  # 数据缺失：保持 open，不打断链
            stop, take = pos.get("stop_loss"), pos.get("take_profit")
            if isinstance(stop, (int, float)) and (bar["open"] <= stop or bar["low"] <= stop):
                price = bar["open"] if bar["open"] <= stop else stop
                close_position(pos, price, valid_for, "gap_stop" if bar["open"] <= stop else "stop_loss")
                exits.append(pos)
            elif isinstance(take, (int, float)) and bar["high"] >= take:
                close_position(pos, take, valid_for, "take_profit")
                exits.append(pos)
            else:
                pos["last_price"] = bar["close"]

        # 2) 本日订单意图
        fills, pendings = [], []
        codes = sorted({a["ts_code"] for a in intents.get("actions", [])})
        bars = get_bars(con, valid_for, codes)
        for i, action in enumerate(intents.get("actions", [])):
            ref = f"{intents_path.name}#actions[{i}]"
            bar = bars.get(action["ts_code"])
            if action["action"] == "buy":
                entry, reason = buy_fill(action, bar)
                if entry is not None:
                    state["positions"].append({
                        "ts_code": action["ts_code"], "pct": float(action["position_pct"]),
                        "cost_price": round(entry, 4),
                        "last_price": round(bar["close"], 4) if bar else round(entry, 4),
                        "filled_date": valid_for, "source_order_ref": ref,
                        "stop_loss": action.get("stop_loss"), "take_profit": action.get("take_profit"),
                        "status": "open",
                    })
                    fills.append(f"buy {action['ts_code']} @{entry:.2f} ({reason})")
                elif reason == "pending_market_data":
                    pendings.append(f"buy {action['ts_code']} 数据缺失")
                    state["open_orders"].append({**{k: action.get(k) for k in
                        ("ts_code", "action", "limit_price", "position_pct")}, "valid_for": valid_for,
                        "source_order_ref": ref, "status": "pending"})
                else:
                    fills.append(f"buy {action['ts_code']} 未成交（当日过期）")
            elif action["action"] == "sell":
                price, reason = sell_fill(action, bar)
                holding = next((p for p in state["positions"]
                                if p["ts_code"] == action["ts_code"] and p.get("status") == "open"), None)
                if holding is None:
                    pendings.append(f"sell {action['ts_code']} 无持仓，意向记录不迁移")
                    continue
                if price is not None:
                    # sell 的 position_pct 语义为"卖出该持仓的比例"（1.0=清仓）
                    pct = holding["pct"] * float(action["position_pct"])
                    if pct >= holding["pct"] - 1e-9:
                        close_position(holding, price, valid_for, f"sell_intent:{reason}")
                    else:
                        holding["pct"] = round(holding["pct"] - pct, 6)
                    fills.append(f"sell {action['ts_code']} @{price:.2f} ({reason})")
                elif reason == "pending_market_data":
                    pendings.append(f"sell {action['ts_code']} 数据缺失")
                    state["open_orders"].append({**{k: action.get(k) for k in
                        ("ts_code", "action", "limit_price", "position_pct")}, "valid_for": valid_for,
                        "source_order_ref": ref, "status": "pending"})
                else:
                    fills.append(f"sell {action['ts_code']} 未成交（当日过期）")
    finally:
        con.close()

    state["version"] = "portfolio1.0"
    state["updated"] = valid_for
    state["cash_pct"] = round(1.0 - sum(p["pct"] for p in state["positions"]
                                        if p.get("status") == "open"), 6)
    snapshot = state_store.save(target, state, snapshot_date=valid_for, history_dir=history_dir)
    print(f"[fills] {len(fills)} 笔：" + ("；".join(fills) if fills else "无"))
    for p in pendings:
        print(f"[pending] {p}")
    for pos in exits:
        print(f"[exit] {pos['ts_code']} {pos['exit_reason']} @{pos['exit_price']} "
              f"pnl={pos['realized_pnl_pct']:+.2f}%")
    opens = [p for p in state["positions"] if p.get("status") == "open"]
    for pos in opens:
        pnl = (pos["last_price"] - pos["cost_price"]) / pos["cost_price"] * 100 if pos.get("last_price") else 0
        print(f"[open] {pos['ts_code']} pct={pos['pct']} cost={pos['cost_price']} "
              f"last={pos.get('last_price')} 浮盈亏={pnl:+.2f}%")
    print(f"[cash] {state['cash_pct']:.2%}  [snapshot] {snapshot}")


if __name__ == "__main__":
    main()
