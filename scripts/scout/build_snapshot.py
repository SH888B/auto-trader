# -*- coding: utf-8 -*-
"""Scout 加工层（M1，对应 PRD FR-2）。

用法：
    python scripts/scout/build_snapshot.py --date 20261026

输入：data/raw/ 下的原始数据（缺失的历史窗口会自动轻量补拉）。
输出：data/curated/YYYY-MM-DD/
    - limit_flags.csv     涨跌停标记（本地判定主口径：一字/回封/涨停/跌停，含 ST 和北交所）
    - breadth.csv         市场广度指标（涨跌家数、涨停家数、昨日涨停今日溢价等）
    - index_panel.csv     三大指数趋势/形态/量比
    - gain_periods.csv    近 5/10/20 日涨幅与 5 日均成交额（本地计算，0 额外 API）
    - VERIFY_report.md    核对报告：与 limit_list_d / kpl_list 官方口径对账

口径说明：
- 涨停判定以 stk_limit 的 up_limit 为准（close >= up_limit），见 PRD FR-2.1；
- limit_list_d 不含 ST，与我们（含 ST）的差值应在 ST 涨停家数附近，核对报告自动解释；
- "昨日涨停今日溢价"用昨日本地判定集合，不用 limit_list_d（防口径混用）。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tushare_client import ROOT, TushareClient  # noqa: E402

EPS = 0.001           # 价格比较容差（分以下）
# 第一层指数面板（E003 扩展）：全A代表 + 结构指数（成长/风偏/避险/投机）
INDEX_CODES = ["399317.SZ", "000001.SH", "399006.SZ", "000688.SH", "399986.SZ", "932000.CSI", "399001.SZ"]
INDEX_NAMES = {"399317.SZ": "国证A指(全A)", "000001.SH": "上证指数", "399006.SZ": "创业板指",
               "000688.SH": "科创50", "399986.SZ": "中证银行", "932000.CSI": "中证2000",
               "399001.SZ": "深证成指"}
GAIN_WINDOWS = [5, 10, 20]   # 区间涨幅窗口（个交易日）


# --------------------------------------------------------------------------- #
# 数据加载：优先新格式 JSON（data/raw/<api>/<api>_<date>.json），回退旧版 CSV
# --------------------------------------------------------------------------- #
def load_daily_raw(date: str) -> pd.DataFrame | None:
    """加载某日全市场日线。新格式优先，回退 data/raw/daily_<date>.csv 旧文件。

    完整性守卫：日线必须含 open/high/low/pre_close 字段，
    字段不全（如历史遗留的裁剪版 bootstrap 文件）视为缺失，防止下游判定出错。
    """
    required = {"open", "high", "low", "pre_close"}
    payload = TushareClient.load_raw("daily", date)
    if payload and payload.get("items"):
        df = pd.DataFrame(payload["items"], columns=payload["fields"])
        if required.issubset(df.columns):
            return df
    legacy = ROOT / "data" / "raw" / f"daily_{date}.csv"
    if legacy.exists():
        df = pd.read_csv(legacy, dtype={"ts_code": str})
        if required.issubset(df.columns):
            return df
    return None


def load_stk_limit_raw(date: str) -> pd.DataFrame | None:
    payload = TushareClient.load_raw("stk_limit", date)
    if payload and payload.get("items"):
        return pd.DataFrame(payload["items"], columns=payload["fields"])
    return None


def load_stock_names() -> dict[str, str]:
    payload = TushareClient.load_raw("stock_basic", "latest")
    if not payload:
        return {}
    df = pd.DataFrame(payload["items"], columns=payload["fields"])
    return dict(zip(df["ts_code"], df["name"]))


def load_trade_cal() -> list[str]:
    payload = TushareClient.load_raw("trade_cal", "20250101_20261231")
    df = pd.DataFrame(payload["items"], columns=payload["fields"])
    return sorted(df.loc[df["is_open"] == 1, "cal_date"].tolist())


def load_index_history(code: str) -> pd.DataFrame:
    files = sorted((ROOT / "data" / "raw" / "index_daily").glob(f"index_daily_{code}_*.json"))
    if not files:
        return pd.DataFrame()
    payload = json.loads(files[-1].read_text(encoding="utf-8"))
    df = pd.DataFrame(payload["items"], columns=payload["fields"])
    return df.sort_values("trade_date").reset_index(drop=True)


# --------------------------------------------------------------------------- #
# FR-2.1 涨跌停标记（本地判定主口径）
# --------------------------------------------------------------------------- #
def compute_limit_flags(date: str) -> pd.DataFrame:
    """计算某日涨跌停标记。返回字段：ts_code, name, close, pct_chg, amount, up/down_limit, flag。"""
    daily = load_daily_raw(date)
    limits = load_stk_limit_raw(date)
    if daily is None or limits is None:
        raise FileNotFoundError(f"{date}: 缺少 daily 或 stk_limit 原始数据，请先运行 fetch_daily.py")
    names = load_stock_names()

    df = daily.merge(limits[["ts_code", "up_limit", "down_limit"]], on="ts_code", how="inner")
    mapped = df["ts_code"].map(names)
    df["name"] = mapped.where(mapped.notna(), "")  # 避免 float 列 fillna(str) 的 Pandas4Warning
    df["is_limit_up"] = df["close"] >= df["up_limit"] - EPS
    df["is_limit_down"] = df["close"] <= df["down_limit"] + EPS
    # 一字：全天未打开涨停价；回封（T字类）：收在涨停价但盘中跌破过
    df["is_yizi"] = df["is_limit_up"] & (df["low"] >= df["up_limit"] - EPS)
    df["is_huifeng"] = df["is_limit_up"] & ~df["is_yizi"]
    df["flag"] = "正常"
    df.loc[df["is_limit_down"], "flag"] = "跌停"
    df.loc[df["is_huifeng"], "flag"] = "涨停(回封)"
    df.loc[df["is_yizi"], "flag"] = "涨停(一字)"
    return df[["ts_code", "name", "close", "pct_chg", "amount", "up_limit", "down_limit", "flag"]]


# --------------------------------------------------------------------------- #
# FR-2.2 市场广度
# --------------------------------------------------------------------------- #
def compute_breadth(date: str, cal: list[str]) -> dict[str, float]:
    """广度指标；昨涨停今日溢价使用昨日本地判定集合（防口径混用）。"""
    flags_today = compute_limit_flags(date)
    daily_today = load_daily_raw(date)
    idx = cal.index(date)
    prev_date = cal[idx - 1]

    metrics: dict[str, float] = {
        "上涨家数": int((daily_today["pct_chg"] > 0).sum()),
        "下跌家数": int((daily_today["pct_chg"] < 0).sum()),
        "涨停家数(含ST)": int(flags_today["flag"].str.startswith("涨停").sum()),
        "跌停家数(含ST)": int((flags_today["flag"] == "跌停").sum()),
        "涨幅>5%家数": int((daily_today["pct_chg"] > 5).sum()),
        "两市成交额(亿元)": round(daily_today["amount"].sum() / 1e5, 1),
    }

    # 昨日涨停今日反馈（两个口径：开盘溢价 / 收盘结果）
    prev_daily = load_daily_raw(prev_date)
    prev_limits = load_stk_limit_raw(prev_date)
    if prev_daily is not None and prev_limits is not None:
        prev_merged = prev_daily.merge(
            prev_limits[["ts_code", "up_limit", "down_limit"]], on="ts_code")
        prev_lu = prev_merged.loc[prev_merged["close"] >= prev_merged["up_limit"] - EPS, "ts_code"]
        fb = daily_today[daily_today["ts_code"].isin(set(prev_lu))]
        if len(fb):
            metrics["昨涨停家数"] = int(len(prev_lu))
            metrics["昨涨停今开盘均价涨幅%"] = round(
                float(fb["open"].sub(fb["pre_close"]).div(fb["pre_close"]).mean() * 100), 2)
            metrics["昨涨停今收盘均价涨幅%"] = round(float(fb["pct_chg"].mean()), 2)
        prev_ld = prev_merged.loc[prev_merged["close"] <= prev_merged["down_limit"] + EPS, "ts_code"]
        if len(prev_ld):
            fb2 = daily_today[daily_today["ts_code"].isin(set(prev_ld))]
            metrics["昨跌停今收盘均价涨幅%"] = round(float(fb2["pct_chg"].mean()), 2) if len(fb2) else None
    return metrics


# --------------------------------------------------------------------------- #
# FR-2.3 指数面板：趋势 / K线形态 / 量比
# --------------------------------------------------------------------------- #
def _candle_pattern(row: pd.Series) -> str:
    """识别大阳线/大阴线/长上影/长下影。规则为近似工程口径，见 PRD FR-2.3。"""
    o, h, l, c = row["open"], row["high"], row["low"], row["close"]
    rng = h - l
    if rng <= 0:
        return "无"
    body = abs(c - o)
    entity = body / rng
    upper = (h - max(o, c)) / rng
    lower = (min(o, c) - l) / rng
    tags = []
    if row["pct_chg"] >= 3 and c > o and entity >= 0.6:
        tags.append("大阳线")
    if row["pct_chg"] <= -3 and c < o and entity >= 0.6:
        tags.append("大阴线")
    if upper >= 0.5 and entity <= 0.35:
        tags.append("长上影")
    if lower >= 0.5 and entity <= 0.35:
        tags.append("长下影")
    return "/".join(tags) if tags else "无"


def compute_index_panel(date: str) -> pd.DataFrame:
    rows = []
    for code in INDEX_CODES:
        df = load_index_history(code)
        if df.empty:
            continue
        df = df[df["trade_date"] <= date].reset_index(drop=True)
        if df.empty or df.iloc[-1]["trade_date"] != date:
            continue
        last = df.iloc[-1]
        closes, vols = df["close"], df["vol"]

        def ma(series: pd.Series, n: int) -> float | None:
            return round(float(series.tail(n).mean()), 2) if len(series) >= n else None

        rows.append({
            "指数": INDEX_NAMES[code], "代码": code,
            "收盘": last["close"], "涨跌幅%": last["pct_chg"],
            "MA5": ma(closes, 5), "MA10": ma(closes, 10), "MA20": ma(closes, 20),
            "量比(vs5日均)": round(float(last["vol"] / vols.tail(6).head(5).mean()), 2) if len(vols) >= 6 else None,
            "K线形态": _candle_pattern(last),
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# FR-2.1b 区间涨幅（近 5/10/20 交易日 + 5 日均成交额）
# --------------------------------------------------------------------------- #
def compute_gain_periods(date: str, cal: list[str], client: TushareClient | None) -> pd.DataFrame:
    need = GAIN_WINDOWS[-1] + 1
    idx = cal.index(date)
    window_dates = cal[max(0, idx - need + 1): idx + 1]
    frames: dict[str, pd.DataFrame] = {}
    for d in window_dates:
        df = load_daily_raw(d)
        if df is None and client is not None:
            # 历史窗口缺失：自动轻量补拉（幂等，只补缺口）
            fields, items = client.call_paginated("daily", {"trade_date": d})
            TushareClient.save_raw("daily", d, {"fields": fields, "items": items})
            data = client.call("stk_limit", {"trade_date": d})
            body = data.get("data") or {}
            TushareClient.save_raw("stk_limit", d,
                                   {"fields": body.get("fields", []), "items": body.get("items", [])})
            df = load_daily_raw(d)
        if df is not None:
            frames[d] = df[["ts_code", "close", "amount"]].copy()
    if len(frames) < 6:
        raise RuntimeError(f"{date}: 历史窗口仅 {len(frames)} 天数据，无法计算区间涨幅")

    ordered_dates = sorted(frames)
    close_df = pd.concat(
        [frames[d].set_index("ts_code")["close"].rename(d) for d in ordered_dates], axis=1).T
    last = close_df.iloc[-1]
    out = pd.DataFrame({"ts_code": close_df.columns})
    for w in GAIN_WINDOWS:
        if len(close_df) > w:
            base = close_df.iloc[-1 - w]
            out[f"近{w}日涨幅%"] = ((last / base - 1) * 100).round(2).values
    amt_df = pd.concat(
        [frames[d].set_index("ts_code")["amount"].rename(d) for d in ordered_dates], axis=1).T
    out["近5日均成交额(亿)"] = (amt_df.tail(5).mean() / 1e5).round(2).values
    return out


# --------------------------------------------------------------------------- #
# 核对报告（PRD §7 验收：与官方口径对账）
# --------------------------------------------------------------------------- #
def write_verify_report(date: str, out_dir: Path, flags: pd.DataFrame,
                        breadth: dict, panel: pd.DataFrame) -> None:
    lu_local = int(flags["flag"].str.startswith("涨停").sum())
    ld_local = int((flags["flag"] == "跌停").sum())

    lines = [f"# M1 核对报告 {date}", "",
             "## 一、涨跌停家数对账（三个口径）", "",
             "| 口径 | 涨停 | 跌停 | 说明 |", "|---|---|---|---|"]
    lines.append(f"| 本地判定（含ST/北交所） | {lu_local} | {ld_local} | 主口径：stk_limit 比对 |")
    payload = TushareClient.load_raw("limit_list_d", f"{date}_up")
    if payload:
        lines.append(f"| limit_list_d（U） | {len(payload['items'])} | — | 官方口径，不含ST |")
    payload = TushareClient.load_raw("limit_list_d", f"{date}_down")
    if payload:
        lines.append(f"| limit_list_d（D） | — | {len(payload['items'])} | 官方口径，不含ST |")
    payload = TushareClient.load_raw("kpl_list", f"{date}_涨停")
    if payload:
        lines.append(f"| kpl_list（开盘啦） | {len(payload['items'])} | — | 次日8:30更新，当日跑可能为空 |")

    lines += ["", "## 二、广度指标", "", "| 指标 | 值 |", "|---|---|"]
    for k, v in breadth.items():
        lines.append(f"| {k} | {v} |")

    lines += ["", "## 三、指数面板", ""]
    lines.append(panel.to_markdown(index=False) if not panel.empty else "（指数数据缺失）")

    lines += ["", "## 四、抽查校验（前5只本地涨停 vs limit_list_d）", ""]
    lld = TushareClient.load_raw("limit_list_d", f"{date}_up")
    if lld:
        lld_df = pd.DataFrame(lld["items"], columns=lld["fields"]).set_index("ts_code")
        sample = flags[flags["flag"].str.startswith("涨停")].head(5)
        lines += ["| 代码 | 名称 | 本地flag | 本地close | 官方close | 官方连板数 | 一致 |",
                  "|---|---|---|---|---|---|---|"]
        for _, r in sample.iterrows():
            if r["ts_code"] in lld_df.index:
                off = lld_df.loc[r["ts_code"]]
                same = abs(float(off["close"]) - float(r["close"])) < 0.011
                lines.append(f"| {r['ts_code']} | {r['name']} | {r['flag']} | {r['close']} "
                             f"| {off['close']} | {off['limit_times']} | {'✅' if same else '❌'} |")
            else:
                lines.append(f"| {r['ts_code']} | {r['name']} | {r['flag']} | {r['close']} | — | — | 官方无此票(可能ST) |")
    (out_dir / "VERIFY_report.md").write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description="Scout 加工层：生成 curated 数据与核对报告")
    parser.add_argument("--date", required=True, help="交易日 YYYYMMDD")
    parser.add_argument("--no-auto-fetch", action="store_true", help="历史窗口缺失时不自动补拉")
    args = parser.parse_args()

    client = None if args.no_auto_fetch else TushareClient(pace=1.5, max_retries=4, timeout=60)
    cal = load_trade_cal()
    if args.date not in cal:
        raise SystemExit(f"{args.date} 不是交易日")

    date_dash = f"{args.date[:4]}-{args.date[4:6]}-{args.date[6:]}"
    out_dir = ROOT / "data" / "curated" / date_dash
    out_dir.mkdir(parents=True, exist_ok=True)

    flags = compute_limit_flags(args.date)
    flags.to_csv(out_dir / "limit_flags.csv", index=False, encoding="utf-8-sig")
    n_lu = int(flags["flag"].str.startswith("涨停").sum())
    n_ld = int((flags["flag"] == "跌停").sum())
    print(f"[limit_flags] 涨停 {n_lu}（一字 {int((flags['flag'] == '涨停(一字)').sum())}）"
          f"/ 跌停 {n_ld} -> limit_flags.csv")

    breadth = compute_breadth(args.date, cal)
    with open(out_dir / "breadth.csv", "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.writer(fp)
        w.writerow(["指标", "值"])
        w.writerows(breadth.items())
    print(f"[breadth] 两市成交额 {breadth.get('两市成交额(亿元)')} 亿 -> breadth.csv")

    panel = compute_index_panel(args.date)
    panel.to_csv(out_dir / "index_panel.csv", index=False, encoding="utf-8-sig")
    print(f"[index_panel] {len(panel)} 个指数 -> index_panel.csv")

    gains = compute_gain_periods(args.date, cal, client)
    gains.to_csv(out_dir / "gain_periods.csv", index=False, encoding="utf-8-sig")
    print(f"[gain_periods] {len(gains)} 只 -> gain_periods.csv")

    write_verify_report(args.date, out_dir, flags, breadth, panel)
    print(f"[verify] -> VERIFY_report.md")
    if client and client.call_log:
        print(f"（历史窗口补拉 {len(client.call_log)} 次调用）")


if __name__ == "__main__":
    main()
