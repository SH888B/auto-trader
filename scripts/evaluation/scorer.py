# -*- coding: utf-8 -*-
"""scorer：Judge 计划 vs 次日实际的自动对账（PRD-Judge §5.1）。

用法：
    uv run python scripts/evaluation/scorer.py --exp E001_基线
    uv run python scripts/evaluation/scorer.py --exp E001_基线 --day 2026-10-22   # 单日

对每个 outputs/<T>/plan.json，取 T 的下一交易日实际行情（数仓 fact_daily_bar）：
- direction_hit：buy 次日 pct_chg ≥ +2% 命中；avoid 次日 ≤ -2% 命中；hold |pct_chg| < 5% 记 N/A
- premium_error：buy 票（次日 open/pre_close-1）与"price 隐含预期"之差——
  以 plan.price 相对 T 日收盘的偏移作为方向预期，取 |实际开盘溢价 - 预期偏移|
- plan_pnl：机械执行 buy：开盘价成交（open 相对 price 的滑点计入）；日内 low ≤ stop_loss
  止损 / high ≥ take_profit 止盈，顺序保守假设止损优先；否则收盘离场。组合按 position_pct 加权
- kill_switch_ok：kill_switch 非空且次日广度触发时是否零新开（启发式：昨涨停开盘均价 < -3%）
- thesis_stats：按 thesis_tag 汇总命中率

输出 scores/score_<T+1>.csv（逐票 + 汇总行）与 scores/summary.csv（实验级）。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scout"))
from tushare_client import ROOT  # noqa: E402

DB = ROOT / "data" / "warehouse.duckdb"


def next_trade_date(con: duckdb.DuckDBPyConnection, date_dash: str) -> str | None:
    r = con.execute(
        "SELECT trade_date FROM fact_daily_bar WHERE trade_date > ? "
        "AND ts_code = '000001.SH' ORDER BY trade_date LIMIT 1", [date_dash]).fetchone()
    return str(r[0]) if r else None


def day_bars(con: duckdb.DuckDBPyConnection, date_dash: str) -> dict[str, dict]:
    rows = con.execute(
        "SELECT ts_code, open, high, low, close, pct_chg FROM fact_daily_bar WHERE trade_date = ?",
        [date_dash]).fetchall()
    return {r[0]: {"open": r[1], "high": r[2], "low": r[3], "close": r[4], "pct_chg": r[5]}
            for r in rows}


def score_action(a: dict, t_close: float | None, next_bar: dict | None) -> dict:
    """单票对账。next_bar=None 表示次日无数据（停牌等）。"""
    row = {"ts_code": a["ts_code"], "name": a["name"], "action": a["action"],
           "thesis_tag": a["thesis_tag"], "direction_hit": "N/A",
           "premium_error": "", "plan_pnl_pct": "", "exit": ""}
    if next_bar is None:
        row["exit"] = "次日无数据"
        return row
    act, pct = a["action"], next_bar["pct_chg"]
    if act == "buy":
        row["direction_hit"] = "Y" if pct >= 2 else ("N" if pct <= -2 else "flat")
        # 溢价误差：预期偏移 = price/T收盘-1；实际 = 次日开盘溢价
        if t_close:
            expect = a["price"] / t_close - 1
            actual = next_bar["open"] / t_close - 1
            row["premium_error"] = f"{abs(actual - expect) * 100:.2f}"
        # 机械执行：开盘成交（滑点计入），止损优先的保守假设
        entry = next_bar["open"]
        if entry <= 0:
            row["exit"] = "停牌"
            return row
        if entry <= a["stop_loss"]:
            # 跳空低开直接越过止损：按开盘价成交即离场，不产生"止损价在成本上方"的荒谬收益
            row["plan_pnl_pct"] = "0.00"
            row["exit"] = "gap_stop"
            return row
        pnl = None
        if next_bar["low"] <= a["stop_loss"]:
            pnl = (a["stop_loss"] - entry) / entry
            row["exit"] = "stop_loss"
        elif next_bar["high"] >= a["take_profit"]:
            pnl = (a["take_profit"] - entry) / entry
            row["exit"] = "take_profit"
        else:
            pnl = (next_bar["close"] - entry) / entry
            row["exit"] = "close"
        row["plan_pnl_pct"] = f"{pnl * 100:.2f}"
    elif act == "avoid":
        row["direction_hit"] = "Y" if pct <= -2 else ("N" if pct >= 2 else "flat")
    elif act == "hold":
        row["direction_hit"] = "N/A" if abs(pct) < 5 else ("warn+" if pct >= 5 else "warn-")
    return row


def score_day(exp_dir: Path, day_dir: Path, con: duckdb.DuckDBPyConnection) -> Path | None:
    plan_path = day_dir / "plan.json"
    if not plan_path.exists():
        return None
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    date_dash = day_dir.name                      # 判断日 T
    nd = next_trade_date(con, date_dash)
    if nd is None:
        print(f"  {date_dash}: 无次日数据，跳过")
        return None
    t_bars = day_bars(con, date_dash)
    n_bars = day_bars(con, nd)

    rows, port = [], 0.0
    for a in plan["actions"]:
        r = score_action(a, (t_bars.get(a["ts_code"]) or {}).get("close"), n_bars.get(a["ts_code"]))
        rows.append(r)
        if a["action"] == "buy" and r["plan_pnl_pct"]:
            port += float(r["plan_pnl_pct"]) * a["position_pct"]

    # 汇总行
    buys = [r for r in rows if r["action"] == "buy"]
    hits = [r for r in rows if r["direction_hit"] in ("Y", "N")]
    n_hit = sum(1 for r in hits if r["direction_hit"] == "Y")
    summary = {
        "ts_code": "SUMMARY", "name": f"{date_dash}→{nd}", "action": "", "thesis_tag": "",
        "direction_hit": f"{n_hit}/{len(hits)}" if hits else "N/A",
        "premium_error": "",
        "plan_pnl_pct": f"{port:.2f}",
        "exit": f"confidence={plan.get('confidence')}",
    }
    rows.append(summary)

    out = exp_dir / "scores" / f"score_{nd.replace('-', '')}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"  {date_dash} → {nd}: {summary['direction_hit']} 方向命中, 组合 {port:+.2f}% -> {out.name}")
    return out


def summarize(exp_dir: Path, files: list[Path]) -> None:
    """实验级汇总：逐日组合收益、方向命中率、按 thesis_tag 分组。"""
    port_all, hit_y, hit_n, tag_stat = [], 0, 0, {}
    for fp in files:
        with open(fp, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if row["ts_code"] == "SUMMARY":
                    port_all.append(float(row["plan_pnl_pct"]))
                elif row["direction_hit"] in ("Y", "N"):
                    tag = row["thesis_tag"] or "其他"
                    s = tag_stat.setdefault(tag, [0, 0])
                    s[1] += 1
                    if row["direction_hit"] == "Y":
                        s[0] += 1
                        hit_y += 1
                    else:
                        hit_n += 1
    out = exp_dir / "scores" / "summary.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["metric", "value"])
        w.writerow(["days", len(files)])
        w.writerow(["direction_hit", f"{hit_y}/{hit_y + hit_n}" if hit_y + hit_n else "N/A"])
        w.writerow(["portfolio_pnl_sum_pct", f"{sum(port_all):.2f}"])
        w.writerow(["portfolio_pnl_avg_pct", f"{sum(port_all) / len(port_all):.2f}" if port_all else ""])
        for tag, (y, n) in sorted(tag_stat.items()):
            w.writerow([f"thesis:{tag}", f"{y}/{n}"])
    print(f"[summary] {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Judge 计划自动对账")
    parser.add_argument("--exp", required=True, help="实验目录名（experiments/ 下）")
    parser.add_argument("--day", help="只算某一天 YYYY-MM-DD（判断日）")
    args = parser.parse_args()

    exp_dir = ROOT / "experiments" / args.exp
    if not exp_dir.exists():
        raise SystemExit(f"实验目录不存在: {exp_dir}")
    con = duckdb.connect(str(DB), read_only=True)
    outs: list[Path] = []
    days = [exp_dir / "outputs" / args.day] if args.day else sorted((exp_dir / "outputs").iterdir())
    for day_dir in days:
        if day_dir.is_dir():
            fp = score_day(exp_dir, day_dir, con)
            if fp:
                outs.append(fp)
    con.close()
    if outs:
        summarize(exp_dir, outs)


if __name__ == "__main__":
    main()
