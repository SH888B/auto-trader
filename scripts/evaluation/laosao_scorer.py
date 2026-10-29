"""老骚盘前计划机械执行评分器（E002）。

口径（见 PRD-E002 §2.5）：
- 老骚的 buy 无价格/止损字段，统一次日开盘价买入、收盘价卖出（裸持一天，保守代理）
- conditional 单：yaml 中 executed: false 的直接跳过；condition: open_below_prev_close 的由本脚本判定
- unavailable: true 的不评分
- watches 记录次日涨跌幅作为"锚定票表现"

用法：
    uv run python scripts/evaluation/laosao_scorer.py
输出：
    experiments/E002_老骚对照/scores/laosao_summary.csv
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import duckdb
import yaml

ROOT = Path(__file__).resolve().parents[2]
PLANS_YAML = ROOT / "experiments/E002_老骚对照/laosao_plans.yaml"
OUT_DIR = ROOT / "experiments/E002_老骚对照/scores"
DB = ROOT / "data/warehouse.duckdb"


def load_bars(con: duckdb.DuckDBPyConnection, date_dash: str) -> dict:
    """取某日全市场 bar，返回 {ts_code: {open, close, pct_chg}}"""
    rows = con.execute(
        "SELECT ts_code, open, close, pct_chg FROM fact_daily_bar WHERE trade_date = ?",
        [date_dash]).fetchall()
    return {r[0]: {"open": r[1], "close": r[2], "pct_chg": r[3]} for r in rows}


def load_prev_close(con: duckdb.DuckDBPyConnection, ts_code: str, date_dash: str):
    """取某票前一交易日收盘（用于 open_below_prev_close 条件判定）"""
    r = con.execute(
        "SELECT close FROM fact_daily_bar WHERE ts_code = ? AND trade_date < ? ORDER BY trade_date DESC LIMIT 1",
        [ts_code, date_dash]).fetchone()
    return r[0] if r else None


def main() -> int:
    plans = yaml.safe_load(PLANS_YAML.read_text(encoding="utf-8"))["plans"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB), read_only=True)

    rows_out = []      # 逐票明细
    day_rows = []      # 逐日组合（等权 executed buys）

    for plan in plans:
        exec_date = plan["exec_date"]
        bars = load_bars(con, exec_date)
        day_buys = []

        for b in plan.get("buys", []):
            row = {"plan_date": plan["plan_date"], "exec_date": exec_date,
                   "window": plan["window"], "ts_code": b["ts_code"], "name": b["name"],
                   "tag": b["tag"], "kind": "buy"}
            if b.get("unavailable"):
                row.update({"executed": "N/A", "entry": "", "exit": "", "pnl_pct": "", "note": "数仓无行情"})
                rows_out.append(row)
                continue
            # 条件判定
            executed = b.get("executed", True)
            note = b.get("exec_note", "")
            if b.get("condition") == "open_below_prev_close":
                prev = load_prev_close(con, b["ts_code"], exec_date)
                bar = bars.get(b["ts_code"])
                executed = bool(prev and bar and bar["open"] < prev)
                note = f"开 {bar['open']} vs 前收 {prev}，{'触发' if executed else '未触发'}"
            bar = bars.get(b["ts_code"])
            if bar is None or bar["open"] <= 0:
                row.update({"executed": str(executed), "entry": "", "exit": "", "pnl_pct": "", "note": "次日无行情/停牌"})
                rows_out.append(row)
                continue
            pnl = (bar["close"] - bar["open"]) / bar["open"] * 100
            row.update({"executed": str(executed), "entry": f"{bar['open']:.2f}",
                        "exit": f"{bar['close']:.2f}", "pnl_pct": f"{pnl:.2f}", "note": note})
            rows_out.append(row)
            if executed:
                day_buys.append(pnl)

        for w in plan.get("watches", []):
            if not w.get("ts_code"):
                continue
            bar = bars.get(w["ts_code"])
            row = {"plan_date": plan["plan_date"], "exec_date": exec_date,
                   "window": plan["window"], "ts_code": w["ts_code"], "name": w["name"],
                   "tag": w.get("role", "观察"), "kind": "watch",
                   "executed": "", "entry": "", "exit": ""}
            row["pnl_pct"] = f"{bar['pct_chg']:.2f}" if bar else ""
            row["note"] = "次日涨跌幅"
            rows_out.append(row)

        day_rows.append({"plan_date": plan["plan_date"], "exec_date": exec_date,
                         "window": plan["window"], "n_buys": len(day_buys),
                         "eq_pnl_pct": f"{sum(day_buys)/len(day_buys):.2f}" if day_buys else ""})

    csv_path = OUT_DIR / "laosao_summary.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
        w.writeheader()
        w.writerows(rows_out)
        w.writerow({})
        w.writerow({"plan_date": "== 逐日组合（等权 executed buys, open->close）==", "exec_date": "", "window": "",
                    "ts_code": "", "name": "", "tag": "", "kind": "", "executed": "", "entry": "", "exit": "",
                    "pnl_pct": "", "note": ""})
        for dr in day_rows:
            w.writerow({"plan_date": dr["plan_date"], "exec_date": dr["exec_date"], "window": dr["window"],
                        "ts_code": f"n={dr['n_buys']}", "name": "", "tag": "", "kind": "day",
                        "executed": "", "entry": "", "exit": "", "pnl_pct": dr["eq_pnl_pct"],
                        "note": "等权收益%"})

    # 控制台输出
    print(f"{'plan':<12}{'exec':<12}{'name':<10}{'tag':<10}{'exec?':<7}{'open->close':<16}{'pnl%':<8}note")
    for r in rows_out:
        print(f"{r['plan_date']:<12}{r['exec_date']:<12}{r['name']:<10}{r['tag']:<10}"
              f"{r['executed']:<7}{r['entry']+'->'+r['exit']:<16}{r['pnl_pct']:<8}{r['note']}")
    print("\n[day] 逐日等权 buy 组合（open->close）：")
    for dr in day_rows:
        print(f"  {dr['plan_date']} → {dr['exec_date']}  [{dr['window']}]  n={dr['n_buys']}  {dr['eq_pnl_pct']}%")
    cmp_days = [float(d["eq_pnl_pct"]) for d in day_rows if d["window"] == "compare" and d["eq_pnl_pct"]]
    if cmp_days:
        print(f"\n[summary] 对照窗（compare）{len(cmp_days)} 天，等权合计 {sum(cmp_days):.2f}%，日均 {sum(cmp_days)/len(cmp_days):.2f}%")
    print(f"[output] {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
