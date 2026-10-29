# -*- coding: utf-8 -*-
"""Mechanically score a v3 Trader order-intent run after valid_for is visible.

Judge semantics are not inferred from one day's PnL. Broker facts, when supplied,
remain separate from the hypothetical Trader replay. Legacy plan.json scoring
stays in scorer.py for E001-E005.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import duckdb
import yaml
from jsonschema import Draft7Validator

ROOT = Path(__file__).resolve().parents[2]


def workspace_path(value: str | Path) -> Path:
    path = Path(value)
    return (ROOT / path).resolve() if not path.is_absolute() else path.resolve()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ref(path: Path) -> dict:
    return {"path": path.relative_to(ROOT).as_posix(), "sha256": sha256(path)}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def validate(instance: dict, schema_name: str) -> None:
    schema = load_json(ROOT / "contracts" / schema_name)
    errors = list(Draft7Validator(schema).iter_errors(instance))
    if errors:
        error = errors[0]
        raise ValueError(f"{schema_name} 校验失败：{'.'.join(map(str, error.path))}: {error.message}")


def parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def get_bars(con: duckdb.DuckDBPyConnection, day: str, codes: list[str]) -> dict[str, dict]:
    if not codes:
        return {}
    placeholders = ",".join("?" for _ in codes)
    rows = con.execute(
        f"SELECT ts_code, open, high, low, close, pre_close, pct_chg "
        f"FROM fact_daily_bar WHERE trade_date = ? AND ts_code IN ({placeholders})",
        [day, *codes]).fetchall()
    return {r[0]: {"open": r[1], "high": r[2], "low": r[3], "close": r[4],
                   "pre_close": r[5], "pct_chg": r[6]} for r in rows}


def replay_buy(action: dict, bar: dict | None, same_bar_priority: str = "stop_loss") -> dict:
    """Replay one buy using only valid_for OHLC; unfilled orders have zero PnL."""
    result = {"status": "pending", "entry_price": None, "exit_price": None,
              "exit_reason": "", "pnl_pct": None,
              "weighted_pnl_pct": 0.0}
    if bar is None:
        return result
    price = action.get("limit_price")
    stop = action.get("stop_loss")
    take = action.get("take_profit")
    if not all(isinstance(v, (int, float)) and v > 0 for v in (price, stop, take)):
        raise ValueError(f"{action.get('ts_code')}: buy 需要正的 limit_price/stop_loss/take_profit")
    if not stop < price < take:
        raise ValueError(f"{action.get('ts_code')}: 必须 stop_loss < limit_price < take_profit")
    if action.get("order_type") == "market":
        entry = float(bar["open"])
    elif float(bar["open"]) <= price:
        entry = float(bar["open"])
    elif float(bar["low"]) <= price <= float(bar["high"]):
        entry = float(price)
    else:
        result.update(status="unfilled", exit_reason="limit_not_touched", pnl_pct=0.0)
        return result
    if entry <= 0:
        result.update(status="invalid_bar", exit_reason="non_positive_open", pnl_pct=None)
        return result

    if float(bar["open"]) <= stop and entry == float(bar["open"]):
        exit_price, reason = float(bar["open"]), "gap_stop"
    elif float(bar["low"]) <= stop:
        exit_price, reason = float(stop), "stop_loss"
    elif float(bar["high"]) >= take:
        exit_price, reason = float(take), "take_profit"
        if same_bar_priority == "stop_loss" and float(bar["low"]) <= stop:
            exit_price, reason = float(stop), "stop_loss"
    else:
        exit_price, reason = float(bar["close"]), "close"
    pnl = (exit_price - entry) / entry * 100
    result.update(status="filled", entry_price=entry, exit_price=exit_price,
                  exit_reason=reason, pnl_pct=round(pnl, 6),
                  weighted_pnl_pct=round(pnl * float(action.get("position_pct", 0)), 6))
    return result


def broker_facts(broker_dir: Path | None) -> tuple[dict, list[dict]]:
    if broker_dir is None or not broker_dir.exists():
        return {"status": "not_provided", "note": "未提供人工 Broker 产物，不推断实际成交"}, []
    orders_path = broker_dir / "orders.json"
    fills_path = broker_dir / "fills.json"
    orders = load_json(orders_path) if orders_path.exists() else {"orders": []}
    fills = load_json(fills_path) if fills_path.exists() else {"fills": []}
    if orders_path.exists():
        validate(orders, "broker_orders.schema.json")
    if fills_path.exists():
        validate(fills, "fills.schema.json")
    facts = {
        "status": "scored" if orders_path.exists() or fills_path.exists() else "not_provided",
        "orders": ref(orders_path) if orders_path.exists() else None,
        "fills": ref(fills_path) if fills_path.exists() else None,
        "order_count": len(orders.get("orders", [])),
        "fill_count": len(fills.get("fills", [])),
        "buy_notional": round(sum(float(f["quantity"]) * float(f["price"])
                                  for f in fills.get("fills", []) if f["side"] == "buy"), 4),
        "sell_notional": round(sum(float(f["quantity"]) * float(f["price"])
                                   for f in fills.get("fills", []) if f["side"] == "sell"), 4),
        "fees": round(sum(float(f.get("fee") or 0) for f in fills.get("fills", [])), 4),
        "note": "实际组合收益还需要完整持仓与资金流水；此处只汇总执行事实",
    }
    return facts, [ref(p) for p in (orders_path, fills_path) if p.exists()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluator：v3 订单意图与人工成交分层评分")
    parser.add_argument("--order-intents", required=True)
    parser.add_argument("--broker-dir", help="包含 orders.json/fills.json 的目录")
    parser.add_argument("--evaluation-config", default="configs/evaluation/discipline.yaml")
    parser.add_argument("--output-dir", help="默认 <arm>/scores")
    args = parser.parse_args()

    intent_path = workspace_path(args.order_intents)
    config_path = workspace_path(args.evaluation_config)
    for path in (intent_path, config_path):
        if not path.is_file() or not path.is_relative_to(ROOT):
            raise SystemExit(f"输入不存在或越界：{path}")
    intents = load_json(intent_path)
    validate(intents, "order_intents.schema.json")
    as_of, valid_for = intents["as_of"], intents["valid_for"]
    if parse_day(valid_for) <= parse_day(as_of):
        raise SystemExit("valid_for 必须晚于 as_of")
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    review_path = workspace_path(intents["judge_review_ref"]["path"])
    if not review_path.is_file() or sha256(review_path) != intents["judge_review_ref"]["sha256"]:
        raise SystemExit("Judge review 引用不存在或 hash 不匹配")
    review = load_json(review_path)
    validate(review, "review.schema.json")
    if review["as_of"] != as_of:
        raise SystemExit("Judge review 与 order intents 的 as_of 不一致")

    broker_dir = workspace_path(args.broker_dir) if args.broker_dir else intent_path.parent.parent / "broker"
    output_dir = workspace_path(args.output_dir) if args.output_dir else intent_path.parent.parent / "scores"
    if not output_dir.is_relative_to(ROOT):
        raise SystemExit("output-dir 必须位于 workspace 内")
    output_path = output_dir / "score.json"
    csv_path = output_dir / "order_intent_replay.csv"
    if output_path.exists() or csv_path.exists():
        raise SystemExit(f"Score 已存在，拒绝覆盖：{output_dir}")

    codes = sorted({a["ts_code"] for a in intents.get("actions", [])})
    db = ROOT / "data" / "warehouse.duckdb"
    con = duckdb.connect(str(db), read_only=True)
    try:
        bars = get_bars(con, valid_for, codes)
    finally:
        con.close()

    rows = []
    for action in intents.get("actions", []):
        row = {"ts_code": action["ts_code"], "action": action["action"],
               "position_pct": action["position_pct"], "status": "not_scored",
               "entry_price": "", "exit_price": "", "exit_reason": "",
               "pnl_pct": "", "weighted_pnl_pct": ""}
        if action["action"] == "buy":
            scored = replay_buy(action, bars.get(action["ts_code"]),
                                str(config.get("same_bar_priority", "stop_loss")))
            row.update(scored)
            if scored["pnl_pct"] is None:
                row["pnl_pct"] = ""
        else:
            row["exit_reason"] = "v3 最小评分器不推断 sell/hold/cancel 收益"
        rows.append(row)

    buy_rows = [r for r in rows if r["action"] == "buy"]
    pending = any(r["status"] == "pending" for r in buy_rows)
    portfolio_pnl = round(sum(float(r["weighted_pnl_pct"] or 0) for r in buy_rows), 6)
    broker, broker_refs = broker_facts(broker_dir)
    scorer_path = Path(__file__).resolve()
    score = {
        "score_version": "v3.0",
        "as_of": as_of,
        "valid_for": valid_for,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "pending_market_data" if pending else "complete",
        "inputs": {"order_intents": ref(intent_path), "judge_review": ref(review_path),
                   "evaluation_config": ref(config_path), "scorer": ref(scorer_path),
                   "broker_artifacts": broker_refs},
        "assumptions": {
            "fill_rule": config.get("fill_rule", "open_or_limit_touch"),
            "same_bar_priority": config.get("same_bar_priority", "stop_loss"),
            "slippage_bps": config.get("slippage_bps", 0),
            "weighted_pnl": "sum(position_pct × single_order_pnl_pct)；未成交贡献为 0，不代表含资金利息的账户收益",
        },
        "layers": {
            "judge": {"status": "structure_only", "schema_valid": True,
                      "review_hash_matches": True, "theme_count": len(review.get("themes", [])),
                      "carrier_count": len(review.get("carriers", [])),
                      "data_gap_count": len(review.get("data_gaps", [])),
                      "note": "市场状态、阶段与载体语义质量需独立评价，不用单日盈亏替代"},
            "trader": {"status": "pending" if pending else "scored",
                       "buy_count": len(buy_rows),
                       "filled_count": sum(r["status"] == "filled" for r in buy_rows),
                       "unfilled_count": sum(r["status"] == "unfilled" for r in buy_rows),
                       "hypothetical_portfolio_pnl_pct": portfolio_pnl,
                       "unscored_orders": [
                           {"ts_code": a["ts_code"], "action": a["action"],
                            "reason": "v3 最小评分器只评 buy；sell/hold/cancel 显式标记 unscored，禁止以 0 冒充无贡献（Evaluator 重构 backlog）"}
                           for a in intents.get("actions", []) if a["action"] != "buy"
                       ],
                       "no_trade": not intents.get("actions"),
                       "no_trade_reason": intents.get("no_trade_reason")},
            "broker": broker,
            "overall": {"status": "partial", "hypothetical_trader_pnl_pct": portfolio_pnl,
                        "actual_account_pnl_pct": None,
                        "note": "实际账户收益未由fills单独推断；执行与策略保持分层"},
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(score, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [
            "ts_code", "action", "position_pct", "status", "entry_price", "exit_price",
            "exit_reason", "pnl_pct", "weighted_pnl_pct"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"[score] {output_path} status={score['status']}")
    print(f"[trader] hypothetical_pnl={portfolio_pnl:+.4f}% buys={len(buy_rows)}")
    print(f"[broker] {broker['status']}")


if __name__ == "__main__":
    main()
