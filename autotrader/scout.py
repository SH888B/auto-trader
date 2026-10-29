"""Scout：数据层服务（取数 → 加工 → 数仓 → 筛选 → 快照冻结）。

由 scripts/scout/ 7 个脚本迁入（DESIGN-V4 §六-4），计算与落盘逻辑逐行等价。
有意的变更仅限：
- 模块常量统一来自 core.WORKSPACE_ROOT；TushareClient 位于 autotrader.relay；
- 原 argparse main() → class Scout 方法；catchup 的 replay 不再 subprocess 调脚本，
  直接调用本类方法；
- finalize 的 derived_code_version / 代码 hash 改指 autotrader/ 实现文件
  （snapshot_version 仍为 s3.0，数据字段口径不变）；
- 数据库连接统一 try/finally 关闭（原脚本依赖进程退出回收）。
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import yaml

from .core import WORKSPACE_ROOT, RunLogger, Workspace, load_yaml, sha256_file
from .relay import TushareClient

ROOT = WORKSPACE_ROOT
RAW = ROOT / "data" / "raw"
DB_PATH = ROOT / "data" / "warehouse.duckdb"
SCHEMA_PATH = ROOT / "contracts" / "schema_v1.sql"
MEMBER_RAW = RAW / "ths_member"

EPS = 0.001           # 价格比较容差（分以下）
# 第一层指数面板（E003 扩展）：全A代表 + 结构指数（成长/风偏/避险/投机）
INDEX_CODES = ["399317.SZ", "000001.SH", "399006.SZ", "000688.SH", "399986.SZ", "932000.CSI",
               "399001.SZ"]
INDEX_NAMES = {"399317.SZ": "国证A指(全A)", "000001.SH": "上证指数", "399006.SZ": "创业板指",
               "000688.SH": "科创50", "399986.SZ": "中证银行", "932000.CSI": "中证2000",
               "399001.SZ": "深证成指"}
GAIN_WINDOWS = [5, 10, 20]   # 区间涨幅窗口（个交易日）

# 每日管道必需的接口及其 raw 文件命名规则（key 生成器，来自 catchup.py）。
# daily 特殊：大 payload，需分页 + 抢救式重试，不在此表（由 fetch_full_date 处理）。
# limit_step（连板天梯）2026-10-28 用户拍板保留并入回补清单。
DAILY_APIS: list[tuple[str, Any]] = [
    ("stk_limit", lambda d: ({"trade_date": d}, d)),
    ("limit_list_d", lambda d: ({"trade_date": d, "limit_type": "U"}, f"{d}_up")),
    ("limit_list_d", lambda d: ({"trade_date": d, "limit_type": "D"}, f"{d}_down")),
    ("kpl_list", lambda d: ({"trade_date": d, "tag": "涨停"}, f"{d}_涨停")),
    ("limit_cpt_list", lambda d: ({"trade_date": d}, d)),
    ("limit_step", lambda d: ({"trade_date": d}, d)),
]

# 入库行的 ingested_at 时间戳；load_warehouse 每次调用时刷新（原脚本为进程级常量）。
_NOW = datetime.now()


# --------------------------------------------------------------------------- #
# 取数层（原 fetch_daily.py）
# --------------------------------------------------------------------------- #
def _norm_items(data: dict) -> dict:
    """把官方响应整理成统一落盘格式：{fields, items}。"""
    body = data.get("data") or {}
    return {"fields": body.get("fields", []), "items": body.get("items", [])}


def ensure_trade_cal(client: TushareClient, force: bool = False) -> list[str]:
    """确保交易日历缓存存在（2025-01 起两年），返回全部交易日列表 YYYYMMDD。"""
    cached = client.load_raw("trade_cal", "20250101_20261231")
    if cached is None or force:
        data = client.call("trade_cal", {"exchange": "SSE", "start_date": "20250101",
                                         "end_date": "20261231"})
        cached = _norm_items(data)
        client.save_raw("trade_cal", "20250101_20261231", cached, force=True)
    cal_date_idx = cached["fields"].index("cal_date")
    open_idx = cached["fields"].index("is_open")
    return sorted(row[cal_date_idx] for row in cached["items"] if row[open_idx] == 1)


def ensure_weekly_caches(client: TushareClient) -> None:
    """周更缓存：证券主档、同花顺概念板块清单。已存在则跳过。

    尽力而为：转接高负载时名称类缓存可能拉不动，不阻塞核心行情采集，
    缺名称时下游 name 列留空，不影响涨跌停判定与核对。
    """
    if client.load_raw("stock_basic", "latest") is None:
        try:
            fields, items = client.call_paginated("stock_basic", {"list_status": "L"},
                                                  page_size=4000)
            client.save_raw("stock_basic", "latest", {"fields": fields, "items": items},
                            force=True)
            print("stock_basic 缓存已建立")
        except Exception as exc:
            print(f"[警告] stock_basic 缓存失败（不阻塞）: {exc}")
    if client.load_raw("ths_index", "concept_N") is None:
        try:
            data = client.call("ths_index", {"type": "N"})
            client.save_raw("ths_index", "concept_N", _norm_items(data), force=True)
            print("ths_index 概念清单已建立")
        except Exception as exc:
            print(f"[警告] ths_index 概念清单失败（不阻塞）: {exc}")


def ensure_index_history(client: TushareClient, end_date: str, days: int = 120) -> None:
    """指数日线区间缓存：各指数拉取最近 days 个自然日，存 index_daily_<code>_range。"""
    start = (datetime.strptime(end_date, "%Y%m%d") - timedelta(days=days)).strftime("%Y%m%d")
    for code in ["000001.SH", "399001.SZ", "399006.SZ",
                 "399317.SZ",   # 国证A指：全A代表（E003，中证全指 000985 无数据，用户批准替代）
                 "000688.SH",   # 科创50：科技/成长/风偏
                 "399986.SZ",   # 中证银行：风偏降低（哑铃端）
                 "932000.CSI"]:  # 中证2000：投机/情绪先行指标（微盘股替代）
        if client.load_raw("index_daily", f"{code}_{start}_{end_date}") is None:
            data = client.call("index_daily", {"ts_code": code, "start_date": start,
                                               "end_date": end_date})
            client.save_raw("index_daily", f"{code}_{start}_{end_date}",
                            _norm_items(data), force=True)


def fetch_full_date(client: TushareClient, date: str) -> dict[str, int]:
    """对单个交易日做全量采集，返回 {api: 行数}。"""
    stats: dict[str, int] = {}

    def save(api: str, key: str, payload: dict, force: bool = False) -> None:
        client.save_raw(api, key, payload, force=force)

    # 1) 全市场日线（分页，PRD §3.2：单页 5000 上限）
    fields, items = client.call_paginated("daily", {"trade_date": date})
    save("daily", date, {"fields": fields, "items": items, "pages": "auto"})
    stats["daily"] = len(items)

    # 2) 每日指标（若也被 5000 截断则自动翻页）
    fields, items = client.call_paginated("daily_basic", {"trade_date": date})
    save("daily_basic", date, {"fields": fields, "items": items})
    stats["daily_basic"] = len(items)

    # 3) 涨跌停价格（涨停判定基准）
    data = client.call("stk_limit", {"trade_date": date})
    body = _norm_items(data)
    save("stk_limit", date, body)
    stats["stk_limit"] = len(body["items"])

    # 4) 涨跌停结构：涨停池 + 跌停池（不含 ST，作细节层）
    for limit_type, tag in [("U", "up"), ("D", "down")]:
        data = client.call("limit_list_d", {"trade_date": date, "limit_type": limit_type})
        body = _norm_items(data)
        save("limit_list_d", f"{date}_{tag}", body)
        stats[f"limit_list_d_{tag}"] = len(body["items"])

    # 5) 连板天梯（fallback 端点，8000 积分档已测可用）
    data = client.call("limit_step", {"trade_date": date}, timeout=120)
    body = _norm_items(data)
    save("limit_step", date, body)
    stats["limit_step"] = len(body["items"])

    # 6) 开盘啦涨跌停榜单（fallback；当日数据次日 8:30 更新，可能为空属正常）
    data = client.call("kpl_list", {"trade_date": date, "tag": "涨停"}, timeout=120)
    body = _norm_items(data)
    save("kpl_list", f"{date}_涨停", body)
    stats["kpl_list"] = len(body["items"])

    # 7) 涨停最强板块统计（fallback；同花顺概念口径，板块榜数据源）
    data = client.call("limit_cpt_list", {"trade_date": date}, timeout=120)
    body = _norm_items(data)
    save("limit_cpt_list", date, body)
    stats["limit_cpt_list"] = len(body["items"])

    return stats


def fetch_light_date(client: TushareClient, date: str) -> dict[str, int]:
    """轻量采集：仅日线 + 涨跌停价。用于历史回补（区间涨幅所需历史窗口）。"""
    stats: dict[str, int] = {}
    fields, items = client.call_paginated("daily", {"trade_date": date})
    client.save_raw("daily", date, {"fields": fields, "items": items, "pages": "auto"})
    stats["daily"] = len(items)
    data = client.call("stk_limit", {"trade_date": date})
    body = _norm_items(data)
    client.save_raw("stk_limit", date, body)
    stats["stk_limit"] = len(body["items"])
    return stats


# --------------------------------------------------------------------------- #
# 加工层（原 build_snapshot.py）：raw → curated
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
    legacy = RAW / f"daily_{date}.csv"
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
    files = sorted((RAW / "index_daily").glob(f"index_daily_{code}_*.json"))
    if not files:
        return pd.DataFrame()
    payload = json.loads(files[-1].read_text(encoding="utf-8"))
    df = pd.DataFrame(payload["items"], columns=payload["fields"])
    return df.sort_values("trade_date").reset_index(drop=True)


def compute_limit_flags(date: str) -> pd.DataFrame:
    """计算某日涨跌停标记。返回字段：ts_code, name, close, pct_chg, amount, up/down_limit, flag。"""
    daily = load_daily_raw(date)
    limits = load_stk_limit_raw(date)
    if daily is None or limits is None:
        raise FileNotFoundError(f"{date}: 缺少 daily 或 stk_limit 原始数据，请先运行 fetch_daily")
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
        prev_ld = prev_merged.loc[
            prev_merged["close"] <= prev_merged["down_limit"] + EPS, "ts_code"]
        if len(prev_ld):
            fb2 = daily_today[daily_today["ts_code"].isin(set(prev_ld))]
            metrics["昨跌停今收盘均价涨幅%"] = (
                round(float(fb2["pct_chg"].mean()), 2) if len(fb2) else None)
    return metrics


def _candle_pattern(row: pd.Series) -> str:
    """识别大阳线/大阴线/长上影/长下影。规则为近似工程口径，见 PRD FR-2.3。"""
    o, h, low, c = row["open"], row["high"], row["low"], row["close"]
    rng = h - low
    if rng <= 0:
        return "无"
    body = abs(c - o)
    entity = body / rng
    upper = (h - max(o, c)) / rng
    lower = (min(o, c) - low) / rng
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
            "量比(vs5日均)": round(float(last["vol"] / vols.tail(6).head(5).mean()), 2)
            if len(vols) >= 6 else None,
            "K线形态": _candle_pattern(last),
        })
    return pd.DataFrame(rows)


def compute_gain_periods(date: str, cal: list[str],
                         client: TushareClient | None) -> pd.DataFrame:
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
                                   {"fields": body.get("fields", []),
                                    "items": body.get("items", [])})
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


def write_verify_report(date: str, out_dir: Path, flags: pd.DataFrame,
                        breadth: dict, panel: pd.DataFrame) -> None:
    """核对报告（PRD §7 验收：与官方口径对账）。"""
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
        lines.append(f"| kpl_list（开盘啦） | {len(payload['items'])} | — | "
                     "次日8:30更新，当日跑可能为空 |")

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
                             f"| {off['close']} | {off['limit_times']} | "
                             f"{'✅' if same else '❌'} |")
            else:
                lines.append(f"| {r['ts_code']} | {r['name']} | {r['flag']} | {r['close']} "
                             f"| — | — | 官方无此票(可能ST) |")
    (out_dir / "VERIFY_report.md").write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 数仓入库层（原 load_to_warehouse.py）
# --------------------------------------------------------------------------- #
def to_date(series: pd.Series) -> pd.Series:
    """YYYYMMDD(str/int) -> DATE。非法值置 NaT，由调用方决定是否报错。"""
    return pd.to_datetime(series.astype(str), format="%Y%m%d", errors="coerce").dt.date


def frame_from_raw(api: str, key: str) -> pd.DataFrame | None:
    """读取 raw JSON 为 DataFrame；不存在或为空返回 None。"""
    path = RAW / api / f"{api}_{key}.json"
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload.get("items") or []
    if not items:
        return None
    return pd.DataFrame(items, columns=payload["fields"])


def _rel_raw(path: Path) -> str:
    """证据文件名（相对 data/raw/）。"""
    return str(path.relative_to(RAW))


def build_fact_daily_bar(date: str) -> pd.DataFrame | None:
    df = frame_from_raw("daily", date)
    if df is None or not {"open", "high", "low"}.issubset(df.columns):
        return None
    df = df.copy()
    df["trade_date"] = to_date(df["trade_date"])
    df["source_file"] = f"daily/daily_{date}.json"
    df["ingested_at"] = _NOW
    return df[["trade_date", "ts_code", "open", "high", "low", "close",
               "pre_close", "change", "pct_chg", "vol", "amount",
               "source_file", "ingested_at"]]


def build_index_rows() -> pd.DataFrame | None:
    """指数日线：raw 中 index_daily_<code>_<start>_<end>.json 全量载入（幂等去重）。"""
    frames = []
    for path in sorted((RAW / "index_daily").glob("index_daily_*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        items = payload.get("items") or []
        if not items:
            continue
        df = pd.DataFrame(items, columns=payload["fields"])
        df["trade_date"] = to_date(df["trade_date"])
        df["source_file"] = _rel_raw(path)
        df["ingested_at"] = _NOW
        frames.append(df[["trade_date", "ts_code", "close", "open", "high", "low",
                          "pre_close", "change", "pct_chg", "vol", "amount",
                          "source_file", "ingested_at"]])
    if not frames:
        return None
    out = pd.concat(frames, ignore_index=True)
    return out.drop_duplicates(subset=["trade_date", "ts_code"], keep="last")


def build_fact_stk_limit(date: str) -> pd.DataFrame | None:
    df = frame_from_raw("stk_limit", date)
    if df is None:
        return None
    df = df.copy()
    df["trade_date"] = to_date(df["trade_date"])
    df["source_file"] = f"stk_limit/stk_limit_{date}.json"
    df["ingested_at"] = _NOW
    return df[["trade_date", "ts_code", "up_limit", "down_limit", "source_file", "ingested_at"]]


def build_fact_limit_flag(date: str) -> pd.DataFrame | None:
    path = ROOT / "data" / "curated" / f"{date[:4]}-{date[4:6]}-{date[6:]}" / "limit_flags.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path, dtype={"ts_code": str})
    df["trade_date"] = to_date(pd.Series([date] * len(df)))
    df["source_file"] = f"curated/{path.parent.name}/limit_flags.csv"
    df["ingested_at"] = _NOW
    return df[["trade_date", "ts_code", "flag", "source_file", "ingested_at"]]


def build_fact_limit_detail(date: str) -> pd.DataFrame | None:
    frames = []
    for tag in ["up", "down"]:
        df = frame_from_raw("limit_list_d", f"{date}_{tag}")
        if df is None:
            continue
        df = df.copy()
        df["trade_date"] = to_date(df["trade_date"])
        df["source_file"] = f"limit_list_d/limit_list_d_{date}_{tag}.json"
        df["ingested_at"] = _NOW
        frames.append(df[["trade_date", "ts_code", "limit", "industry", "close", "pct_chg",
                          "amount", "fd_amount", "float_mv", "turnover_ratio",
                          "first_time", "last_time", "open_times",
                          "up_stat", "limit_times", "name", "source_file", "ingested_at"]]
                      .rename(columns={"limit": "limit_type"}))
    return pd.concat(frames, ignore_index=True) if frames else None


def build_fact_limit_step(date: str) -> pd.DataFrame | None:
    df = frame_from_raw("limit_step", date)
    if df is None:
        return None
    df = df.copy()
    df["trade_date"] = to_date(df["trade_date"])
    df["source_file"] = f"limit_step/limit_step_{date}.json"
    df["ingested_at"] = _NOW
    return df[["trade_date", "ts_code", "name", "nums", "source_file", "ingested_at"]]


def build_fact_board_rank(date: str) -> pd.DataFrame | None:
    df = frame_from_raw("limit_cpt_list", date)
    if df is None:
        return None
    df = df.copy()
    df["trade_date"] = to_date(df["trade_date"])
    df["source_file"] = f"limit_cpt_list/limit_cpt_list_{date}.json"
    df["ingested_at"] = _NOW
    df = df.rename(columns={"ts_code": "board_code", "name": "board_name"})
    return df[["trade_date", "board_code", "board_name", "rank", "pct_chg",
               "up_nums", "cons_nums", "up_stat", "days", "source_file", "ingested_at"]]


def build_fact_kpl_list(date: str) -> pd.DataFrame | None:
    df = frame_from_raw("kpl_list", f"{date}_涨停")
    if df is None:
        return None
    df = df.copy()
    df["trade_date"] = to_date(df["trade_date"])
    df["source_file"] = f"kpl_list/kpl_list_{date}_涨停.json"
    df["ingested_at"] = _NOW
    keep = ["trade_date", "ts_code", "tag", "status", "theme", "lu_desc", "lu_time",
            "open_time", "last_time", "limit_order", "lu_limit_order", "amount",
            "turnover_rate", "free_float", "net_change", "source_file", "ingested_at"]
    return df[[c for c in keep if c in df.columns]]


def build_fact_breadth(date: str) -> pd.DataFrame | None:
    path = ROOT / "data" / "curated" / f"{date[:4]}-{date[4:6]}-{date[6:]}" / "breadth.csv"
    if not path.exists():
        return None
    kv = pd.read_csv(path)
    m = dict(zip(kv["指标"], kv["值"]))
    row = {
        "trade_date": to_date(pd.Series([date]))[0],
        "up_cnt": m.get("上涨家数"), "down_cnt": m.get("下跌家数"),
        "limit_up_cnt": m.get("涨停家数(含ST)"), "limit_down_cnt": m.get("跌停家数(含ST)"),
        "gt5_cnt": m.get("涨幅>5%家数"), "total_amount_yi": m.get("两市成交额(亿元)"),
        "prev_lu_cnt": m.get("昨涨停家数"),
        "prev_lu_open_premium": m.get("昨涨停今开盘均价涨幅%"),
        "prev_lu_close_premium": m.get("昨涨停今收盘均价涨幅%"),
        "prev_ld_feedback": m.get("昨跌停今收盘均价涨幅%"),
        "source_file": f"curated/{path.parent.name}/breadth.csv", "ingested_at": _NOW,
    }
    return pd.DataFrame([row])


def build_dim_stock() -> pd.DataFrame | None:
    df = frame_from_raw("stock_basic", "latest")
    if df is None:
        return None
    df = df.copy()
    df["list_date"] = to_date(df["list_date"])
    df["updated_at"] = _NOW
    # stock_basic 接口不回传 list_status（它是请求参数），上市状态由请求条件保证为 L
    df["list_status"] = "L"
    keep = ["ts_code", "name", "industry", "market", "list_date", "list_status", "updated_at"]
    return df[[c for c in keep if c in df.columns]]


TABLE_BUILDERS = {
    "fact_daily_bar": build_fact_daily_bar,
    "fact_stk_limit": build_fact_stk_limit,
    "fact_limit_flag": build_fact_limit_flag,
    "fact_limit_detail": build_fact_limit_detail,
    "fact_limit_step": build_fact_limit_step,
    "fact_board_rank": build_fact_board_rank,
    "fact_kpl_list": build_fact_kpl_list,
    "fact_breadth": build_fact_breadth,
}


def upsert(con: duckdb.DuckDBPyConnection, table: str, df: pd.DataFrame,
           dates: list[str]) -> int:
    """幂等入库：先按日期删除旧行，再插入。

    append 是按列位置匹配的，迁移加列后位置可能漂移——这里显式把 df 列序
    对齐到表结构，杜绝 BIGINT→TIMESTAMP 这类错位列转换。
    """
    if df is None or df.empty:
        return 0
    date_list = ", ".join(f"DATE '{d[:4]}-{d[4:6]}-{d[6:]}'" for d in dates)
    con.execute(f"DELETE FROM {table} WHERE trade_date IN ({date_list})")
    table_cols = [r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()]
    df = df[[c for c in table_cols if c in df.columns]]
    con.append(table, df)
    return len(df)


# --------------------------------------------------------------------------- #
# 筛选层（原 select_stocks.py）：数仓 → derived 榜单
# --------------------------------------------------------------------------- #
def universe_filter(df: pd.DataFrame, universe: dict,
                    names: dict[str, str] | None) -> pd.DataFrame:
    """universe.yaml 过滤：市场前缀 + ST（有名称数据时）。

    Returns:
        过滤后的 DataFrame；过滤动作记入调用方日志。

    备注：停牌（当日无成交）天然被排除在 derived 之外，无需显式过滤。
    """
    prefixes = [p for m in universe.get("markets", {}).values() for p in m.get("code_prefix", [])]
    if prefixes:
        df = df[df["ts_code"].str[:3].isin(prefixes) | df["ts_code"].str[:2].isin(prefixes)]
    if universe.get("exclusions", {}).get("st_stock") and names:
        mask = df["ts_code"].map(lambda c: "ST" in (names.get(c) or "").upper())
        df = df[~mask]
    return df


# --------------------------------------------------------------------------- #
# 快照冻结（原 finalize_snapshot.py）
# --------------------------------------------------------------------------- #
def payload_items(path: Path) -> list:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload.get("items"), list):
        return payload["items"]
    data = payload.get("data") or {}
    return data.get("items") or []


def csv_rows(path: Path) -> int:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return max(sum(1 for _ in csv.reader(handle)) - 1, 0)


def code_hash() -> str:
    """Scout 实现文件的组合 hash（原 scripts_hash，改指 autotrader/ 实现）。"""
    digest = hashlib.sha256()
    for path in sorted((ROOT / "autotrader").glob("*.py")):
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# 历史回补（原 catchup.py）
# --------------------------------------------------------------------------- #
def load_trade_days(client: TushareClient, start: str, end: str) -> list[str]:
    """返回 [start, end] 的交易日清单（is_open=1，YYYYMMDD 升序）。

    trade_cal 是静态参考数据，且转接守卫要求单次 ≥200 行（窄区间查询会被误判空壳），
    因此策略=全量缓存：本地 data/raw/trade_cal/ 有缓存直接读，没有则拉全年落盘。
    """
    cache = RAW / "trade_cal" / "trade_cal_full.json"
    if cache.exists():
        payload = json.loads(cache.read_text(encoding="utf-8"))
    else:
        year = start[:4]
        data = client.call("trade_cal",
                           {"start_date": f"{year}0101", "end_date": f"{year}1231",
                            "is_open": "1"},
                           timeout=60)
        body = data.get("data") or {}
        payload = {"fields": body.get("fields", []), "items": body.get("items", [])}
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        print(f"[trade_cal] 全年缓存落盘 {len(payload['items'])} 行 -> {cache}")
    idx = payload["fields"].index("cal_date")
    return sorted(str(r[idx]) for r in payload["items"] if start <= str(r[idx]) <= end)


def missing_files(date: str) -> list[tuple[str, str, dict]]:
    """返回该日缺失的 (api, key, params) 清单。

    kpl_list 空响应视为缺失（次日增强未到位，需重拉）；
    limit_list_d 空响应同样视为缺失（Tushare 原生接口当晚应到位，空=转接窗口问题）。
    """
    out = []
    for api, key_fn in DAILY_APIS:
        params, key = key_fn(date)
        path = RAW / api / f"{api}_{key}.json"
        if not path.exists():
            out.append((api, key, params))
        elif api in ("kpl_list", "limit_list_d"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not payload.get("items"):
                out.append((api, key, params))
    return out


def prev_trade_day(client: TushareClient, start: str) -> str | None:
    """start 之前最近的一个交易日（昨涨停反馈依赖前一交易日 stk_limit）。"""
    probe = (datetime.strptime(start, "%Y%m%d") - timedelta(days=10)).strftime("%Y%m%d")
    days = [d for d in load_trade_days(client, probe, start) if d < start]
    return days[-1] if days else None


def fetch_daily_rescue(client: TushareClient, date: str, rounds: int = 8,
                       timeout: int = 45) -> int:
    """日线抢救：短超时 × 多轮抓转接"好窗口"，两页分页（第二页 572 行全科创的历史教训）。

    来自 temp/daily_rescue.py 的验证过的策略，并入正式管道。
    返回落盘行数；全部轮次失败返回 0（调用方记录缺口）。
    """
    def page(offset: int) -> tuple[list, list] | None:
        params = {"trade_date": date}
        if offset:
            params["offset"] = str(offset)
        try:
            data = client.call("daily", params, timeout=timeout)
            body = data.get("data") or {}
            return body.get("fields", []), body.get("items", [])
        except Exception as exc:
            print(f"    offset={offset}: {str(exc)[:80]}")
            return None

    for attempt in range(1, rounds + 1):
        r1 = page(0)
        if r1 is None:
            continue
        fields, items = r1
        if len(items) == 5000:  # 达到单页上限，必有第二页
            r2 = page(5000)
            if r2 is None:
                print(f"    第{attempt}轮: 第一页 5000 行到手，第二页失败，重试")
                continue
            items = items + r2[1]
        client.save_raw("daily", date, {"fields": fields, "items": items}, force=True)
        print(f"  [daily] {date}: {len(items)} 行（第 {attempt} 轮成功）")
        return len(items)
    print(f"  [daily] {date}: {rounds} 轮均失败，留待下次")
    return 0


def fetch_small(client: TushareClient, api: str, key: str, params: dict,
                timeout: int = 120) -> bool:
    """打板类小 payload 接口：标准退避（client 内置），成功落盘返回 True。"""
    try:
        data = client.call(api, params, timeout=timeout)
        body = data.get("data") or {}
        client.save_raw(api, key, {"fields": body.get("fields", []),
                                   "items": body.get("items", [])})
        print(f"  [{api}] {key}: {len(body.get('items', []))} 行")
        return True
    except Exception as exc:
        print(f"  [{api}] {key}: 失败 {str(exc)[:80]}")
        return False


# --------------------------------------------------------------------------- #
# 板块成分取数入库（原 fetch_theme_members.py）
# --------------------------------------------------------------------------- #
def raw_member_path(board_code: str, stamp: str) -> Path:
    """stamp = YYYYMMDD 快照戳。"""
    return MEMBER_RAW / f"ths_member_{board_code}_{stamp}.json"


def raw_age_days(path: Path) -> float | None:
    if not path.exists():
        return None
    return (datetime.now().timestamp() - path.stat().st_mtime) / 86400


def latest_rank_date() -> str:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        row = con.execute("SELECT MAX(trade_date) FROM fact_board_rank").fetchone()
    finally:
        con.close()
    if not row or row[0] is None:
        raise RuntimeError("fact_board_rank 为空，无法确定默认快照日")
    return row[0].strftime("%Y%m%d")


def load_boards_from_rank(date: str) -> list[str]:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        rows = con.execute(
            "SELECT DISTINCT board_code FROM fact_board_rank WHERE trade_date = ? "
            "ORDER BY board_code",
            [datetime.strptime(date, "%Y%m%d").date()],
        ).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def fetch_boards(client: TushareClient, boards: list[str], stamp: str, *,
                 max_age_days: float, force: bool) -> tuple[list[str], list[str]]:
    """按快照戳逐个板块取 ths_member(is_new=Y)。返回 (本次取数, 跳过)。"""
    fetched, skipped = [], []
    for i, code in enumerate(boards, 1):
        path = raw_member_path(code, stamp)
        if not force:
            age = raw_age_days(path)
            if age is not None and age <= max_age_days:
                skipped.append(code)
                print(f"[{i}/{len(boards)}] {code} 跳过（{stamp} 快照 {age:.1f} 天前已取）")
                continue
        print(f"[{i}/{len(boards)}] {code} 取数（戳 {stamp}）…")
        data = client.call("ths_member", {"ts_code": code, "is_new": "Y"})
        payload = {"fields": (data.get("data") or {}).get("fields", []),
                   "items": (data.get("data") or {}).get("items", [])}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        fetched.append(code)
        print(f"           {len(payload['items'])} 只成分 -> {path.relative_to(ROOT)}")
    return fetched, skipped


def ingest_members(boards: list[str], stamp: str) -> int:
    """把 (板块, 快照戳) 的 raw 灌入 dim_board_member（幂等：先按戳删后插）。

    兼容未带日期的遗留 raw（ths_member_<code>.json）：按本次 stamp 入库。
    """
    stamp_date = datetime.strptime(stamp, "%Y%m%d").date()
    frames = []
    for code in boards:
        path = raw_member_path(code, stamp)
        legacy = MEMBER_RAW / f"ths_member_{code}.json"
        src = path if path.exists() else (legacy if legacy.exists() else None)
        if src is None:
            continue
        payload = json.loads(src.read_text(encoding="utf-8"))
        items = payload.get("items") or []
        if not items:
            continue
        df = pd.DataFrame(items, columns=payload["fields"])
        df = df[["con_code", "con_name"]].copy()
        df["trade_date"] = stamp_date
        df["board_code"] = code
        df["is_new"] = "Y"
        df["source_file"] = f"ths_member/{src.name}"
        df["ingested_at"] = datetime.now()
        frames.append(df[["trade_date", "board_code", "con_code", "con_name", "is_new",
                          "source_file", "ingested_at"]])
    if not frames:
        return 0
    df = pd.concat(frames, ignore_index=True).drop_duplicates(
        subset=["trade_date", "board_code", "con_code"])
    con = duckdb.connect(str(DB_PATH))
    try:
        con.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        codes = ", ".join(f"'{c}'" for c in sorted(set(df["board_code"])))
        con.execute(f"DELETE FROM dim_board_member WHERE trade_date = DATE '{stamp_date}' "
                    f"AND board_code IN ({codes})")
        con.append("dim_board_member", df)
    finally:
        con.close()
    return len(df)


# --------------------------------------------------------------------------- #
# Scout 服务：原 7 个脚本的 main() 收为方法
# --------------------------------------------------------------------------- #
class Scout:
    """Scout 数据层服务：取数、加工、数仓、筛选、快照冻结、回补、板块成分。

    所有方法幂等（raw 只追加跳过、数仓先删后插、快照可重冻结），
    失败抛异常，由 pipeline 层决定是否中断链路。
    """

    def __init__(self, workspace: Workspace | None = None,
                 logger: RunLogger | None = None) -> None:
        self.ws = workspace or Workspace()
        self.logger = logger or RunLogger()

    # ------------------------------------------------------------------ #
    # 取数层
    # ------------------------------------------------------------------ #
    def fetch_daily(self, dates: list[str], *, light: bool = False,
                    force: bool = False) -> None:
        """对若干交易日做全量/轻量采集（原 fetch_daily.py main）。

        Args:
            dates: 交易日 YYYYMMDD 列表；非交易日自动跳过。
            light: 轻量模式：仅日线 + 涨跌停价（历史回补窗口用）。
            force: 强制重抓（绕过"已 ok 跳过"，也绕过未到可用时间的 not_due）。
        """
        from .fetch import Fetcher, FetchIncomplete

        client = TushareClient(pace=1.5, max_retries=5, timeout=180)
        trading_days = ensure_trade_cal(client)
        wanted = [d for d in dates if d in trading_days]
        for d in dates:
            if d not in trading_days:
                print(f"[跳过] {d} 不是交易日")
        if not wanted:
            raise ValueError("没有可抓取的交易日")

        if not light:
            ensure_weekly_caches(client)
            ensure_index_history(client, max(wanted))

        fetcher = Fetcher(workspace=self.ws, client_factory=lambda: client)
        t0 = time.monotonic()
        summaries = fetcher.sync_many(
            wanted, window="all", force=force,
            only=["daily", "stk_limit"] if light else None)
        for summary in summaries:
            counts: dict[str, int] = {}
            for record in summary["results"].values():
                counts[record["status"]] = counts.get(record["status"], 0) + 1
            print(f"[完成] {summary['date']}: "
                  + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
            readiness = summary["readiness"]
            if readiness["status"] == "blocked":
                print(f"  [blocked] 缺 {readiness['blocking']}")

        log = client.write_call_log(wanted[0])
        n_calls = len(client.call_log)
        errors = sum(1 for c in client.call_log if "error" in c)
        print(f"\n调用 {n_calls} 次（含重试），失败 {errors} 次，耗时 {time.monotonic() - t0:.0f}s")
        print(f"调用日志: {log}")

        blocked = [s["date"] for s in summaries if s["readiness"]["status"] == "blocked"]
        if blocked:
            raise FetchIncomplete(blocked, summaries[-1]["readiness"])

    # ------------------------------------------------------------------ #
    # 加工层
    # ------------------------------------------------------------------ #
    def build_snapshot(self, date: str, *, auto_fetch: bool = True) -> None:
        """raw → curated：涨跌停标记、广度、指数面板、区间涨幅、核对报告。"""
        client = None if not auto_fetch else TushareClient(pace=1.5, max_retries=4, timeout=60)
        cal = load_trade_cal()
        if date not in cal:
            raise ValueError(f"{date} 不是交易日")

        date_dash = f"{date[:4]}-{date[4:6]}-{date[6:]}"
        out_dir = ROOT / "data" / "curated" / date_dash
        out_dir.mkdir(parents=True, exist_ok=True)

        flags = compute_limit_flags(date)
        flags.to_csv(out_dir / "limit_flags.csv", index=False, encoding="utf-8-sig")
        n_lu = int(flags["flag"].str.startswith("涨停").sum())
        n_ld = int((flags["flag"] == "跌停").sum())
        print(f"[limit_flags] 涨停 {n_lu}（一字 {int((flags['flag'] == '涨停(一字)').sum())}）"
              f"/ 跌停 {n_ld} -> limit_flags.csv")

        breadth = compute_breadth(date, cal)
        with open(out_dir / "breadth.csv", "w", newline="", encoding="utf-8-sig") as fp:
            writer = csv.writer(fp)
            writer.writerow(["指标", "值"])
            writer.writerows(breadth.items())
        print(f"[breadth] 两市成交额 {breadth.get('两市成交额(亿元)')} 亿 -> breadth.csv")

        panel = compute_index_panel(date)
        panel.to_csv(out_dir / "index_panel.csv", index=False, encoding="utf-8-sig")
        print(f"[index_panel] {len(panel)} 个指数 -> index_panel.csv")

        gains = compute_gain_periods(date, cal, client)
        gains.to_csv(out_dir / "gain_periods.csv", index=False, encoding="utf-8-sig")
        print(f"[gain_periods] {len(gains)} 只 -> gain_periods.csv")

        write_verify_report(date, out_dir, flags, breadth, panel)
        print("[verify] -> VERIFY_report.md")
        if client and client.call_log:
            print(f"（历史窗口补拉 {len(client.call_log)} 次调用）")

    # ------------------------------------------------------------------ #
    # 数仓入库层
    # ------------------------------------------------------------------ #
    def load_warehouse(self, date: str | None = None, *, all_dates: bool = False) -> None:
        """raw/curated → DuckDB 数仓（幂等，先按日期删后插）。"""
        global _NOW  # 每次调用刷新 ingested_at（原脚本为进程级常量）
        _NOW = datetime.now()

        if all_dates:
            dates = sorted({m.group(1) for p in (RAW / "daily").glob("daily_*.json")
                            if (m := re.search(r"(\d{8})", p.name))})
        elif date:
            dates = [date]
        else:
            raise ValueError("需指定 date 或 all_dates=True")

        con = duckdb.connect(str(DB_PATH))
        try:
            con.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
            # 轻量迁移：老库补 v1.1 新增列（ADD COLUMN IF NOT EXISTS 幂等）
            for col, typ in [("float_mv", "DOUBLE"), ("turnover_ratio", "DOUBLE"),
                             ("name", "VARCHAR")]:
                con.execute(f"ALTER TABLE fact_limit_detail ADD COLUMN IF NOT EXISTS {col} {typ}")

            total = 0
            for table, builder in TABLE_BUILDERS.items():
                frames = [f for d in dates if (f := builder(d)) is not None and not f.empty]
                if not frames:
                    # 原始文件缺失时清掉该日数仓残留，避免陈旧数据
                    # （实测：kpl raw 移走后重跑，数仓旧行让 digest 误报 v2）
                    date_list = ", ".join(f"DATE '{d[:4]}-{d[4:6]}-{d[6:]}'" for d in dates)
                    n_del = con.execute(
                        f"DELETE FROM {table} WHERE trade_date IN ({date_list})").fetchone()[0]
                    if n_del:
                        print(f"[清理] {table}: 删除 {n_del} 行陈旧数据（raw 缺失）")
                    continue
                df = pd.concat(frames, ignore_index=True)
                n = upsert(con, table, df, dates)
                total += n
                print(f"[入库] {table}: {n} 行")

            # 指数与维度表：全量刷新（自带去重）
            for table, builder in [("fact_daily_bar_idx", build_index_rows),
                                   ("dim_stock", build_dim_stock)]:
                df = builder()
                if df is None or df.empty:
                    print(f"[跳过] {table}: 无数据")
                    continue
                if table == "fact_daily_bar_idx":
                    # 指数并入行情总表（同构）；删除条件按本次入库的指数代码集合，
                    # 不可硬编码代码清单（新指数入库时会漏删导致重跑主键冲突），
                    # 也不可加交易所后缀过滤（399001.SZ/399006.SZ 会被 '%.SH' 漏掉）
                    dlist = ", ".join(f"DATE '{d}'"
                                      for d in sorted(set(df["trade_date"].astype(str))))
                    codes = ", ".join(f"'{c}'" for c in sorted(set(df["ts_code"].astype(str))))
                    con.execute(f"DELETE FROM fact_daily_bar WHERE trade_date IN ({dlist}) "
                                f"AND ts_code IN ({codes})")
                    con.append("fact_daily_bar", df)
                else:
                    con.execute("DELETE FROM dim_stock")
                    con.append("dim_stock", df)
                total += len(df)
                print(f"[入库] {table}: {len(df)} 行")

            print(f"\n合计 {total} 行 -> {DB_PATH}")
            print([t for t, in con.execute("SHOW TABLES").fetchall()])
        finally:
            # DuckDB append 的注册对象可能是 Table 也可能是 View（版本差异），按类型清理
            obj_type = con.execute(
                "SELECT 'view' FROM duckdb_views WHERE view_name='__append_df'").fetchone()
            drop_sql = ("DROP VIEW IF EXISTS __append_df" if obj_type
                        else "DROP TABLE IF EXISTS __append_df")
            con.execute(drop_sql)
            con.close()

    # ------------------------------------------------------------------ #
    # 筛选层
    # ------------------------------------------------------------------ #
    def select_stocks(self, date: str) -> None:
        """数仓 → data/snapshots/<date>/derived/*.csv 标准榜单。"""
        date_dash = f"{date[:4]}-{date[4:6]}-{date[6:]}"
        out_dir = ROOT / "data" / "snapshots" / date_dash / "derived"
        out_dir.mkdir(parents=True, exist_ok=True)

        selectors = load_yaml(ROOT / "configs" / "data" / "selectors.yaml")
        universe = load_yaml(ROOT / "configs" / "data" / "universe.yaml")
        sel_ver = selectors.get("version", "?")

        con = duckdb.connect(str(DB_PATH), read_only=True)
        try:
            self._select_stocks_impl(date, date_dash, out_dir, selectors, universe, sel_ver, con)
        finally:
            con.close()

    def _select_stocks_impl(self, date: str, date_dash: str, out_dir: Path,
                            selectors: dict, universe: dict, sel_ver: str,
                            con: duckdb.DuckDBPyConnection) -> None:
        today = con.execute(
            # 数仓 fact_daily_bar 混有指数行（7 只），全部显式排除
            #（000688 会误过 universe 前缀过滤）
            "SELECT b.* FROM fact_daily_bar b WHERE b.trade_date = ? AND b.ts_code NOT IN "
            "('000001.SH','399001.SZ','399006.SZ','399317.SZ','000688.SH','399986.SZ','932000.CSI')",
            [date_dash]).fetchdf()
        if today.empty:
            raise RuntimeError(f"{date}: 数仓中无当日行情，请先运行 build + load_to_warehouse")

        names: dict[str, str] = {}
        dim = con.execute("SELECT ts_code, name FROM dim_stock").fetchdf()
        if not dim.empty:
            names = dict(zip(dim["ts_code"], dim["name"]))
        mapped = today["ts_code"].map(names)
        # 避免 float 列 fillna(str) 的 Pandas4Warning
        today["name"] = mapped.where(mapped.notna(), "")

        pool = universe_filter(today, universe, names)
        st_note = '启用' if names else '降级:无名称数据'
        print(f"[universe] {len(today)} -> {len(pool)} 只（ST过滤 {st_note}）")

        def save(df: pd.DataFrame, name: str, note: str) -> None:
            df = df.copy()
            df.insert(0, "selectors_version", sel_ver)
            df.to_csv(out_dir / f"{name}.csv", index=False, encoding="utf-8-sig")
            print(f"[derived] {name}.csv: {len(df)} 行（{note}）")

        groups = selectors.get("groups", {})
        # 成交额 / 涨幅 / 跌幅 Top
        save(pool.nlargest(groups["amount_top"]["n"], "amount"), "amount_top", "成交额榜")
        gain = pool[pool["pct_chg"] > 0].nlargest(groups["gain_top"]["n"], "pct_chg")
        save(gain, "gain_top", "涨幅榜")
        loss = pool.nsmallest(groups["loss_top"]["n"], "pct_chg")
        save(loss, "loss_top", "跌幅榜")
        # E003 新增三组（s0.3）
        pit_cfg = groups.get("pit_top", {})
        if pit_cfg:
            min_amt = float(pit_cfg.get("min_amount_yi", 1.0)) * 1e5  # 亿元→千元
            cand = pool[pool["amount"] >= min_amt].copy()
            # E006 人工审核修订：坑 = 收盘距高点回落（负值越大越坑），而非高低振幅
            cand["收盘回落%"] = ((cand["close"] - cand["high"]) / cand["high"] * 100).round(2)
            save(cand.nsmallest(int(pit_cfg.get("n", 20)), "收盘回落%"),
                 "pit_top", "当日坑货榜（收盘距高点回落%）")
        gp_path = ROOT / "data" / "curated" / date_dash / "gain_periods.csv"
        gp = pd.read_csv(gp_path) if gp_path.exists() else pd.DataFrame()
        gw_cfg = groups.get("gain_week", {})
        gw_col = f"近{gw_cfg.get('window', 5)}日涨幅%"
        if gw_cfg and not gp.empty and gw_col in gp.columns:
            gw = gp.nlargest(int(gw_cfg.get("n", 30)), gw_col).merge(
                pool[["ts_code", "name", "pct_chg", "amount"]], on="ts_code", how="left")
            save(gw, "gain_week", "周涨幅榜（查漏补缺近期强弱）")
        cc_cfg = groups.get("cm20_core", {})
        if cc_cfg and not gp.empty:
            m = gp[gp["ts_code"].str[:3].isin([str(p) for p in cc_cfg["prefix"]])]
            m = m[(m["近20日涨幅%"] >= float(cc_cfg.get("min_gain20_pct", 20)))
                  & (m["近5日均成交额(亿)"] >= float(cc_cfg.get("min_amount5_yi", 5.0)))]
            cm = m.nlargest(int(cc_cfg.get("n", 20)), "近20日涨幅%").merge(
                pool[["ts_code", "name", "pct_chg", "amount"]], on="ts_code", how="left")
            save(cm, "cm20_core", "20cm题材中军候选（F007）")
        # 涨停 / 跌停：口径由 selectors.yaml limit_source 决定（用户拍板 2026-10-27）
        limit_cfg = selectors.get("limit_source", {})
        primary = limit_cfg.get("primary", "official")
        kpl_enrich = limit_cfg.get("kpl_enrich", True)

        def kpl_join(df: pd.DataFrame) -> pd.DataFrame:
            """kpl 增强：题材/封单/最大封单/连板状态（仅增强已有代码，不补 ST，用户拍板）。"""
            if not kpl_enrich:
                return df
            kpl = con.execute(
                "SELECT ts_code, theme, status, limit_order, lu_limit_order FROM fact_kpl_list "
                "WHERE trade_date = ? AND tag = '涨停'", [date_dash]).fetchdf()
            if kpl.empty:
                return df
            return df.merge(kpl, on="ts_code", how="left")

        if primary == "official":
            # 官方 limit_list_d 口径（非 ST）：开板次数/几天几板/封单/封板时间/市值/换手
            lu = con.execute(
                "SELECT * FROM fact_limit_detail WHERE trade_date = ? AND limit_type = 'U'",
                [date_dash]).fetchdf()
            if not lu.empty:
                lu["amount"] = lu["amount"] / 1000  # 官方 amount 单位是元，统一为千元（daily 口径）
                lu["structure"] = lu.apply(
                    lambda r: "未开板(一字/T字)" if r["open_times"] == 0
                    else f"开板{int(r['open_times'])}次回封", axis=1)
                lu = lu.sort_values("fd_amount", ascending=False)
            save(kpl_join(lu), "limit_up", "官方口径+kpl增强" if kpl_enrich else "官方口径(非ST)")
            # 涨停聚焦表（jv1.2 用户拍板 2026-10-28）：涨停太多时按规则聚焦，替代全量平铺
            focus_cfg = selectors.get("groups", {}).get("limit_up_focus", {})
            if not lu.empty and focus_cfg:
                picks: list[tuple[str, str]] = []
                used: set[str] = set()

                def pick(df_: pd.DataFrame, tag: str, n: int) -> None:
                    for code in df_.head(n)["ts_code"]:
                        if code not in used:
                            used.add(code)
                            picks.append((code, tag))

                fd_n = int(focus_cfg.get("fd_top_n", 10))
                pick(lu.nlargest(fd_n, "fd_amount"), "封单最大", fd_n)
                max_lt = int(lu["limit_times"].max())
                pick(lu[lu["limit_times"] == max_lt], f"最高板{max_lt}", 10)
                cm_lu = lu[lu["ts_code"].str[:3].isin(["300", "301", "688", "689"])]
                pick(cm_lu[cm_lu["limit_times"] >= 2].sort_values("limit_times", ascending=False),
                     "20cm连板", 20)
                leaders = (lu.sort_values(["limit_times", "fd_amount"], ascending=[False, False])
                           .groupby("industry").head(1))
                pick(leaders, "行业最高板(题材代理)", int(focus_cfg.get("industry_leader_n", 15)))
                fb_lu = lu[lu["limit_times"] == 1]
                each_n = int(focus_cfg.get("first_board_each_n", 2))
                for col, tag in (("fd_amount", "首板封单王"), ("amount", "首板成交王"),
                                 ("float_mv", "首板市值王")):
                    pick(fb_lu.nlargest(each_n, col), tag, each_n)
                pick(cm_lu.sort_values(["limit_times", "fd_amount"], ascending=[False, False]),
                     "20cm", 20)
                focus = lu.merge(pd.DataFrame(picks, columns=["ts_code", "关注类别"]), on="ts_code")
                focus = focus.set_index("ts_code").loc[[p[0] for p in picks]].reset_index()
                save(focus, "limit_up_focus",
                     "涨停聚焦：封单最大/最高板/20cm连板/行业最高板(题材代理)/首板三王/20cm")
            ld = con.execute(
                "SELECT * FROM fact_limit_detail WHERE trade_date = ? AND limit_type = 'D'",
                [date_dash]).fetchdf()
            if not ld.empty:
                ld["amount"] = ld["amount"] / 1000  # 同上，元→千元
            save(ld.sort_values("amount", ascending=False), "limit_down", "官方口径(非ST)")
        else:
            # local：本地 stk_limit 比对判定（含 ST/北交所，字段粗，保留作 fallback）
            flags = con.execute("SELECT ts_code, flag FROM fact_limit_flag WHERE trade_date = ?",
                                [date_dash]).fetchdf()
            pool = pool.merge(flags, on="ts_code", how="left")
            lu = pool[pool["flag"].str.startswith("涨停", na=False)].sort_values(
                ["flag", "amount"], ascending=[True, False])
            save(lu, "limit_up", "本地判定，含ST/北交所")
            ld = pool[pool["flag"] == "跌停"].sort_values("amount", ascending=False)
            save(ld, "limit_down", "本地判定，含ST/北交所")

        # 昨日涨停/跌停今日反馈（与涨停口径同源）
        prev = con.execute(
            "SELECT trade_date FROM fact_daily_bar WHERE trade_date < ? "
            "ORDER BY trade_date DESC LIMIT 1", [date_dash]).fetchone()
        if prev:
            if primary == "official":
                prev_lu = {r[0] for r in con.execute(
                    "SELECT ts_code FROM fact_limit_detail "
                    "WHERE trade_date = ? AND limit_type = 'U'",
                    [prev[0]]).fetchall()}
                prev_ld = {r[0] for r in con.execute(
                    "SELECT ts_code FROM fact_limit_detail "
                    "WHERE trade_date = ? AND limit_type = 'D'",
                    [prev[0]]).fetchall()}
            else:
                prev_flags = con.execute(
                    "SELECT ts_code, flag FROM fact_limit_flag WHERE trade_date = ?",
                    [prev[0]]).fetchdf()
                prev_lu = set(
                    prev_flags[prev_flags["flag"].str.startswith("涨停", na=False)]["ts_code"])
                prev_ld = set(prev_flags[prev_flags["flag"] == "跌停"]["ts_code"])
            fb = pool[pool["ts_code"].isin(prev_lu)][
                ["ts_code", "name", "open", "pre_close", "pct_chg", "close", "amount"]]
            fb["开盘溢价%"] = ((fb["open"] / fb["pre_close"] - 1) * 100).round(2)
            save(fb.sort_values("pct_chg", ascending=False), "prev_lu_feedback",
                 f"昨涨停今日反馈（{prev[0]} 集合）")
            fb2 = pool[pool["ts_code"].isin(prev_ld)][["ts_code", "name", "pct_chg", "amount"]]
            save(fb2.sort_values("pct_chg"), "prev_ld_feedback", "昨跌停今日反馈")
        # 指数面板：复制 curated 产物
        panel = ROOT / "data" / "curated" / date_dash / "index_panel.csv"
        if panel.exists():
            save(pd.read_csv(panel), "index_panel", "第一层指数量能")
        # 关注列表不在这里落 derived 表：watchlist 是 state，由 Curator 渲染 digest。
        # 但"载体生命周期"的候选集需要关注票，这里按 T-1 冻结快照只读取代码，
        # 不读 current（PIT），缺失则视为空集。
        def _watchlist_codes(day: str) -> set[str]:
            from .core import StateStore

            path = StateStore(self.ws).as_of_path(
                "state/watchlist/judge/current.yaml", day,
                "state/watchlist/judge/history", strict="before")
            if path is None:
                return set()
            state = load_yaml(path)
            return {str(s["ts_code"]) for s in (state.get("stocks") or [])
                    if str(s.get("status", "关注")) == "关注" and s.get("ts_code")}
        # 板块榜（含规模阈值过滤）
        boards = con.execute("SELECT * FROM fact_board_rank WHERE trade_date = ?",
                             [date_dash]).fetchdf()
        min_cons = selectors.get("concept_min_cons", 15)
        top_n = selectors.get("boards_selection_n", 10)
        ths = RAW / "ths_index" / "ths_index_concept_N.json"
        if not boards.empty and ths.exists():
            payload = json.loads(ths.read_text(encoding="utf-8"))
            tdf = pd.DataFrame(payload["items"], columns=payload["fields"])
            size_map = dict(zip(tdf["ts_code"], tdf["count"]))
            boards["cons_count"] = boards["board_code"].map(size_map)
            kept = boards[boards["cons_count"] >= min_cons].nsmallest(top_n, "rank")
            dropped = len(boards) - len(kept)
            # E003：板块三分法标注（theme_class.yaml 是事实源；未收录的板块标"未分类"）
            tc_path = ROOT / "data" / "assets" / "theme_class.yaml"
            tc = load_yaml(tc_path) if tc_path.exists() else {}
            cmap = {c["board"]: c for c in (tc or {}).get("classes", [])}
            kept["板块类型"] = kept["board_name"].map(
                lambda b: (cmap.get(b) or {}).get("type", "未分类"))
            kept["周期位置"] = kept["board_name"].map(
                lambda b: (cmap.get(b) or {}).get("lifecycle", "—"))
            save(kept, "boards", f"板块榜，规模>={min_cons} 过滤掉 {dropped} 个，含三分法分类")

        # ---- E004 延续：天花板两行（F013 客观层）+ 载体生命周期表（F012 记录层客观列）----
        lc_cfg = selectors.get("lifecycle", {})
        ce_cfg = selectors.get("ceiling", {})
        n_back = max(int(lc_cfg.get("lookback_days", 12)), int(ce_cfg.get("window_days", 20)))
        days_back = [r[0] for r in con.execute(
            "SELECT DISTINCT trade_date FROM fact_daily_bar WHERE trade_date <= ? "
            "ORDER BY trade_date DESC LIMIT ?", [date_dash, n_back + 1]).fetchall()]

        def _ph(seq: list) -> str:
            return ",".join("?" * len(seq))

        # A. 天花板：滚动窗口内最高连板（10cm/20cm 双轨），记录保持者与今日突破标记
        if ce_cfg and days_back:
            win = days_back[:int(ce_cfg.get("window_days", 20))]
            cm_min = float(ce_cfg.get("cm_board_min_pct", 15))
            det = con.execute(
                f"SELECT d.trade_date, d.ts_code, d.limit_times, b.pct_chg "
                f"FROM fact_limit_detail d JOIN fact_daily_bar b "
                f"  ON b.trade_date = d.trade_date AND b.ts_code = d.ts_code "
                f"WHERE d.limit_type = 'U' AND d.trade_date IN ({_ph(win)})",
                win).fetchdf()
            rows = []
            for track, sub in (("10cm", det[det["pct_chg"] < cm_min]),
                               ("20cm", det[det["pct_chg"] >= cm_min])):
                if sub.empty:
                    rows.append({"track": track, "window_max": 0, "record_holder": "",
                                 "record_date": "", "today_max": 0, "breaker": ""})
                    continue
                peak = int(sub["limit_times"].max())
                # 最早达到日
                rec = sub[sub["limit_times"] == peak].sort_values("trade_date").iloc[0]
                same_day = sub[(sub["limit_times"] == peak)
                               & (sub["trade_date"] == rec["trade_date"])]
                holder = "、".join(dict.fromkeys(names.get(c, c) for c in same_day["ts_code"]))
                today_sub = sub[sub["trade_date"] == date_dash]
                today_max = int(today_sub["limit_times"].max()) if not today_sub.empty else 0
                prior_max = sub[sub["trade_date"] != date_dash]["limit_times"].max()
                prior = int(prior_max) if pd.notna(prior_max) else 0
                breaker = ""
                if today_max > prior and today_max >= 2:
                    who = "、".join(names.get(c, c) for c in
                                   today_sub[today_sub["limit_times"] == today_max]["ts_code"])
                    breaker = f"★{who} {today_max} 板，首破近 {len(win) - 1} 日纪录（{prior} 板）"
                rows.append({"track": track, "window_max": peak,
                             "record_holder": holder,
                             "record_date": str(rec["trade_date"])[:10],
                             "today_max": today_max, "breaker": breaker})
            save(pd.DataFrame(rows), "ceiling",
                 "天花板双轨（F013）：滚动最高连板/记录保持者/今日突破")

        # B. 载体生命周期表：剧幕规则判定（客观列），模仿关系列留空由 Judge 维护
        if lc_cfg and days_back:
            lb = days_back[:int(lc_cfg.get("lookback_days", 12))]
            det = con.execute(
                f"SELECT trade_date, ts_code, limit_times, fd_amount, open_times, up_stat "
                f"FROM fact_limit_detail WHERE limit_type='U' AND trade_date IN ({_ph(lb)})",
                lb).fetchdf()
            today_det = det[det["trade_date"] == date_dash] if not det.empty else pd.DataFrame()
            cand: set[str] = _watchlist_codes(date_dash)
            if not today_det.empty:
                cand |= set(today_det[today_det["limit_times"] >= 2]["ts_code"])
            recent5 = det[det["trade_date"].isin(lb[:5])] if not det.empty else pd.DataFrame()
            if not recent5.empty:
                cand |= set(recent5[recent5["limit_times"] >= int(
                    lc_cfg.get("break_min_boards", 3))]["ts_code"])
            cand = {c for c in cand if c} - {'000001.SH', '399001.SZ', '399006.SZ'}
            rows = []
            if cand and not det.empty:
                bars = con.execute(
                    f"SELECT trade_date, ts_code, open, high, low, close, pct_chg, amount "
                    f"FROM fact_daily_bar WHERE trade_date IN ({_ph(lb)}) "
                    f"AND ts_code IN ({_ph(sorted(cand))})",
                    lb + sorted(cand),
                ).fetchdf()
                lt_map = {(r.trade_date, r.ts_code): r for r in det.itertuples()}
                band = float(lc_cfg.get("platform_band_pct", 8))
                shrink = float(lc_cfg.get("platform_shrink", 0.6))
                w2g = float(lc_cfg.get("wave2_min_gain", 8))
                exit_dd = float(lc_cfg.get("drawdown_exit_pct", 12))
                stag = int(lc_cfg.get("stagnate_days", 3))
                dv = float(lc_cfg.get("diverge_min_pct", -2.5))
                so_fd = float(lc_cfg.get("soft_open_fd_ratio", 0.5))
                so_amt = float(lc_cfg.get("soft_open_amt_ratio", 2.0))
                for code in sorted(cand):
                    b = bars[bars["ts_code"] == code].sort_values("trade_date")
                    b = b.reset_index(drop=True)
                    if b.empty:
                        continue
                    b["lt"] = [
                        lt_map.get((d, code), None).limit_times if lt_map.get((d, code)) else 0
                        for d in b["trade_date"]
                    ]
                    t = b.iloc[-1]
                    lt_today, max_lt = int(t["lt"]), int(b["lt"].max())
                    # 断板日：最后一次"连板≥break_min 且次日无涨停"
                    brk = None
                    for i in range(len(b) - 1):
                        if b["lt"][i] >= int(lc_cfg.get("break_min_boards", 3)) \
                                and b["lt"][i + 1] == 0:
                            brk = b.iloc[i]
                    # 二波启动日：断板日后首个涨幅 ≥wave2_min 的阳线
                    ws = None
                    if brk is not None:
                        hit = b[(b["trade_date"] > brk["trade_date"]) & (b["pct_chg"] >= w2g)]
                        if not hit.empty:
                            ws = hit.iloc[0]
                    high_close = float(b["close"].max())
                    n_since_high = len(b) - 1 - b.index[b["close"] == high_close].max()
                    limit_days = b[b["lt"] >= 1]
                    last_limit_dt = str(limit_days["trade_date"].iloc[-1]) \
                        if not limit_days.empty else ""
                    # 事件列
                    ev = []
                    if len(b) >= 2 and b["lt"][len(b) - 2] >= 1 and lt_today == 0:
                        ev.append("断板第一天")
                    rec = lt_map.get((t["trade_date"], code))
                    # 参照日：连板中的票取上一个涨停日（封单/量能环比）；已断板的取断板日
                    prev_fd, prev_amt = None, None
                    if lt_today >= 1 and len(limit_days) >= 2:
                        pl = limit_days.iloc[-2]
                        prev_rec = lt_map.get((pl["trade_date"], code))
                        prev_fd = float(prev_rec.fd_amount) if prev_rec is not None \
                            and prev_rec.fd_amount else None
                        prev_amt = float(pl["amount"])
                    elif brk is not None:
                        prev_rec = lt_map.get((brk["trade_date"], code))
                        prev_fd = float(prev_rec.fd_amount) if prev_rec is not None \
                            and prev_rec.fd_amount else None
                        prev_amt = float(brk["amount"])
                    if rec is not None:
                        if int(rec.open_times) >= 1:
                            ev.append(f"开板{int(rec.open_times)}次回封")
                        amt_today = float(t["amount"])
                        if prev_fd and float(rec.fd_amount or 0) / prev_fd < so_fd \
                                and prev_amt and amt_today / prev_amt > so_amt:
                            ev.append("软开板?")
                        if len(b) >= 2 and prev_amt and amt_today / prev_amt > 3:
                            ev.append(f"巨量×{amt_today / prev_amt:.1f}")
                    # 剧幕判定（顺序即优先级；lc0.2：单波边界=5日线，用户拍板 2026-10-28）
                    ma_n = int(lc_cfg.get("wave_ma", 5))
                    b["ma"] = b["close"].rolling(ma_n).mean()
                    t = b.iloc[-1]
                    above = bool(float(t["close"]) >= float(t["ma"])) \
                        if pd.notna(t["ma"]) else True
                    # 本波启动日（窗口内首个涨停日）；破线只认"启动之后"的收盘破均线
                    prior_break = False
                    reclaim_dt = ""
                    if not limit_days.empty:
                        w0 = str(limit_days.iloc[0]["trade_date"])
                        mid = b[(b["trade_date"] > w0)].iloc[:-1]
                        if not mid.empty:
                            brk_mask = mid["close"] < mid["ma"]
                            prior_break = bool(brk_mask.any())
                            if prior_break:
                                # 破线前最后一日=重新站上起点
                                reclaim_dt = str(mid["trade_date"].iloc[-1])
                    n_since_brk = (int((pd.Timestamp(t["trade_date"])
                                        - pd.Timestamp(brk["trade_date"])).days)
                                   if brk is not None else 0)
                    board_min = int(lc_cfg.get("board_min", 2))
                    if max_lt >= board_min and lt_today == 0 and not above:
                        stage, stay = "退潮·破5日线", "1天"   # 主条件：收盘破线=波结束
                    elif max_lt >= board_min and lt_today == 0 and brk is not None \
                            and (float(t["close"]) < float(brk["low"])
                                 or (high_close - float(t["close"])) / high_close * 100
                                 > exit_dd):
                        stage, stay = "退潮", f"断板{n_since_brk}天"
                    elif prior_break and above:
                        stage = "二波·新高" if float(t["close"]) >= high_close else "二波·重启"
                        days2 = (pd.Timestamp(t["trade_date"]) - pd.Timestamp(reclaim_dt)).days
                        stay = f"{days2}天"
                    elif lt_today >= 2 and above:
                        stage, stay = "一波加速", f"{lt_today}连板"
                    elif brk is not None and above and ws is not None:
                        # 未破5日线=仍是同一波
                        stage, stay = "单波·二波尝试", f"断板{n_since_brk}天"
                    elif brk is not None and lt_today == 0 and last_limit_dt >= str(
                            lb[min(4, len(lb) - 1)]) \
                            and abs(float(t["close"]) / float(brk["close"]) - 1) * 100 <= band \
                            and float(t["amount"]) < float(brk["amount"]) * shrink:
                        n = int(
                            (pd.Timestamp(t["trade_date"]) - pd.Timestamp(brk["trade_date"])).days)
                        stage, stay = "高位平台", f"{n}天"
                    elif max_lt >= board_min and lt_today == 0 and above \
                            and n_since_high >= stag \
                            and (float(t["close"]) / high_close - 1) * 100 < 3:
                        stage, stay = "滞涨", f"高点后{n_since_high}天"
                    elif lt_today == 0 and float(t["pct_chg"]) <= dv \
                            and (last_limit_dt >= str(lb[min(4, len(lb) - 1)])
                                 or (float(t["close"]) / float(b["close"].iloc[0]) - 1) * 100
                                 >= 20):
                        stage = "主升分歧"
                        if last_limit_dt:
                            days3 = (pd.Timestamp(t["trade_date"])
                                     - pd.Timestamp(last_limit_dt)).days
                            stay = f"{days3}天"
                        else:
                            stay = ""
                    else:
                        stage, stay = ("观察" if above else "观察·线下"), ""
                    rows.append({
                        "ts_code": code, "name": names.get(code, ""),
                        "载体类型": "20cm" if code[:3] in ("300", "301", "302", "688", "689")
                        else "10cm",
                        "up_stat": rec.up_stat if rec is not None else "",
                        "连板数": lt_today, "剧幕": stage, "停留": stay,
                        "五日线": "线上" if above else "破线",
                        "今日事件": " / ".join(ev), "涨跌幅": round(float(t["pct_chg"]), 2),
                        "成交额亿": round(float(t["amount"]) / 1e5, 2),
                        "封单亿": round(float(rec.fd_amount) / 1e8, 2)
                        if rec is not None and rec.fd_amount else 0,
                        "模仿自": "", "模板剩余": "",
                    })
            save(pd.DataFrame(rows), "carrier_lifecycle",
                 f"载体生命周期表（F012 记录层，规则 {lc_cfg.get('version', '?')}）；"
                 "模仿关系列由 Judge 维护")
        print(f"\n-> {out_dir}")

    # ------------------------------------------------------------------ #
    # 快照冻结层
    # ------------------------------------------------------------------ #
    def finalize(self, date: str) -> dict[str, Any]:
        """冻结某日快照清单 snapshot.yaml（不生成 Digest）。返回 manifest。"""
        if len(date) != 8 or not date.isdigit():
            raise ValueError("date 必须为 YYYYMMDD")
        date_dash = f"{date[:4]}-{date[4:6]}-{date[6:]}"
        snap_dir = ROOT / "data" / "snapshots" / date_dash
        derived_dir = snap_dir / "derived"
        if not derived_dir.is_dir():
            raise RuntimeError(f"缺少 derived 目录，请先运行 select_stocks：{derived_dir}")

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
            derived_files[path.stem] = {**self.ws.artifact_ref(path), "rows": rows}

        from .fetch import Fetcher

        readiness = Fetcher(workspace=self.ws).status(date)
        gaps: list[str] = list(readiness["gaps"])
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
            ("daily", f"daily_{date}.json", True),
            ("stk_limit", f"stk_limit_{date}.json", True),
            ("limit_list_d", f"limit_list_d_{date}_up.json", True),
            ("limit_list_d", f"limit_list_d_{date}_down.json", True),
            ("kpl_list", f"kpl_list_{date}_涨停.json", False),
            ("limit_cpt_list", f"limit_cpt_list_{date}.json", True),
        ]
        raw_inputs = {}
        for api, filename, required in raw_specs:
            path = RAW / api / filename
            key = f"{api}/{filename}"
            if not path.exists():
                continue
            rows = len(payload_items(path))
            raw_inputs[key] = {**self.ws.artifact_ref(path), "rows": rows}

        stock_basic = RAW / "stock_basic"
        if not stock_basic.exists() or not any(stock_basic.glob("*.json")):
            gaps.append("stock_basic 缺失（股票名称/ST 过滤降级）")

        scout_impl = ROOT / "autotrader" / "scout.py"
        derived_code_version = f"scout:{sha256_file(scout_impl)[:16]}"
        generated_day = datetime.now().astimezone().date().isoformat()
        manifest = {
            "snapshot_version": "s3.1",
            "trade_date": date_dash,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "data_as_of": date_dash,
            "derived_code_version": derived_code_version,
            "backfilled": generated_day != date_dash,
            "status": readiness["status"],
            "readiness": {
                "version": readiness.get("readiness_version", "r1.0"),
                "path": f"data/meta/readiness/{date_dash}.yaml",
                "blocking": readiness["blocking"],
                "degraded_by": readiness["degraded_by"],
            },
            "data_recipe": self.ws.artifact_ref(data_recipe_path),
            "selectors": self.ws.artifact_ref(selectors_path),
            "universe": self.ws.artifact_ref(universe_path),
            "selectors_version": selectors.get("version", "?"),
            "universe_version": universe.get("version", "?"),
            "scripts_sha256": code_hash(),
            "derived_rows": derived_rows,
            "derived_files": derived_files,
            "raw_inputs": raw_inputs,
            "availability": {
                "decision_date": date_dash,
                "granularity": "trade_date",
                "note": "文件 hash 与生成时间已锁定；as_of 目前为交易日粒度，精确到秒的"
                        "可用时间隔离仍需数据级 available_at。",
                "late_enrichments": [
                    "kpl_list 可能在交易日之后更新；历史回放须确认其在实际决策前已可用",
                ],
            },
            "gaps": list(dict.fromkeys(gaps)),
        }
        output = snap_dir / "snapshot.yaml"
        output.write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
                          encoding="utf-8")
        print(f"[snapshot] {output} status={manifest['status']}")
        print(f"[derived] {len(derived_files)} 张表，{sum(derived_rows.values())} 行")
        for gap in manifest["gaps"]:
            print(f"  [gap] {gap}")
        return manifest

    # ------------------------------------------------------------------ #
    # 组合：单日完整数据链
    # ------------------------------------------------------------------ #
    def run_date(self, date: str, *, auto_fetch: bool = True,
                 run_id: str | None = None) -> None:
        """单日完整 Scout 链：build → load → select → finalize，带运行日志。"""
        rid = run_id or f"scout-{date}"
        common = {"run_id": rid, "module": "scout",
                  "as_of": f"{date[:4]}-{date[4:6]}-{date[6:]}"}
        started = time.monotonic()
        self.logger.emit(event="start", status="running", **common)
        try:
            from .fetch import Fetcher

            readiness = Fetcher(workspace=self.ws).status(date)
            if readiness["status"] == "blocked" and auto_fetch:
                print(f"[run_date] 数据未就绪，先取数：{readiness['blocking']}")
                self.fetch_daily([date])
            self.build_snapshot(date, auto_fetch=auto_fetch)
            self.load_warehouse(date)
            self.select_stocks(date)
            self.finalize(date)
        except Exception as exc:
            self.logger.emit(event="error", status="failed",
                             duration_ms=int((time.monotonic() - started) * 1000),
                             error=str(exc)[:200], **common)
            raise
        self.logger.emit(event="end", status="success",
                         duration_ms=int((time.monotonic() - started) * 1000),
                         artifact_path=f"data/snapshots/{common['as_of']}/snapshot.yaml", **common)

    # ------------------------------------------------------------------ #
    # 历史回补
    # ------------------------------------------------------------------ #
    def catchup(self, start: str, end: str, *, check: bool = False,
                replay: bool = False) -> None:
        """历史回补：核对缺口 → 抢救式补拉 → 可选链式重建快照（原 catchup.py）。

        Args:
            start/end: 区间 YYYYMMDD（含），非交易日自动剔除。
            check: 只打印缺口清单，不抓取。
            replay: 补完后对区间内有 raw 的交易日重跑完整数据链 + 全量入库。
        """
        if check:
            self._catchup_check(start, end)
            return
        self._catchup_fill(start, end)
        if replay:
            self._catchup_replay(start, end)

    def _catchup_check(self, start: str, end: str) -> None:
        """干跑：打印区间内每个交易日缺哪些文件，不发起抓取。"""
        client = TushareClient(pace=1.0, max_retries=2, timeout=60)
        days = load_trade_days(client, start, end)
        print(f"区间 {start}~{end} 交易日 {len(days)} 天")
        total = 0
        for d in days:
            miss = missing_files(d)
            daily_ok = (RAW / "daily" / f"daily_{d}.json").exists()
            status = []
            if not daily_ok:
                status.append("daily")
            status += [f"{a}_{k}" for a, k, _ in miss]
            total += len(status)
            print(f"  {d}: {'✓ 完整' if not status else '缺 ' + ', '.join(status)}")
        print(f"\n共 {total} 个缺口"
              + ("（需要先补 daily 才能算涨停反馈的日子，stk_limit 等也要齐）" if total else ""))

    def _catchup_fill(self, start: str, end: str) -> None:
        """补缺口主流程：逐交易日核对 → 先救 daily（大 payload）→ 再补打板类小接口。"""
        client = TushareClient(pace=1.2, max_retries=1, timeout=45)
        days = load_trade_days(client, start, end)
        print(f"区间 {start}~{end} 交易日 {len(days)} 天，开始核对缺口")
        # 前馈依赖：区间首日的"昨涨停反馈"需要前一交易日的 stk_limit
        prev = prev_trade_day(client, start)
        if prev and not (RAW / "stk_limit" / f"stk_limit_{prev}.json").exists():
            print(f"前馈依赖：补前一交易日 {prev} 的 stk_limit")
            fetch_small(client, "stk_limit", prev, {"trade_date": prev})
        failed: list[str] = []
        for d in days:
            miss = missing_files(d)
            need_daily = not (RAW / "daily" / f"daily_{d}.json").exists()
            if not need_daily and not miss:
                print(f"{d}: ✓ 完整，跳过")
                continue
            print(f"{d}: 补 {('daily + ' if need_daily else '')}{len(miss)} 个接口")
            if need_daily and not fetch_daily_rescue(client, d):
                failed.append(f"daily_{d}")
                continue  # 日线没到位，打板类补了也没法校验，跳到下一天
            for api, key, params in miss:
                if not fetch_small(client, api, key, params):
                    failed.append(f"{api}_{key}")
        client.write_call_log("catchup")
        if failed:
            print(f"\n[未完成] {len(failed)} 项，转接窗口不佳时可重跑（幂等）：")
            for f in failed:
                print(f"  - {f}")
        else:
            print("\n[完成] 全部缺口补齐")

    def _catchup_replay(self, start: str, end: str) -> None:
        """链式重建：对区间内每个有 raw 数据的交易日跑完整下游管道。"""
        client = TushareClient(pace=1.0, max_retries=1, timeout=10)
        days = [d for d in load_trade_days(client, start, end)
                if (RAW / "daily" / f"daily_{d}.json").exists()]
        if not days:
            raise RuntimeError("区间内无可用日线数据，无法重建")
        for d in days:
            print(f"\n===== 重建 {d} =====")
            self.run_date(d, auto_fetch=False)
        print("\n===== 全量入库 =====")
        self.load_warehouse(all_dates=True)
        print(f"[replay] 完成：{len(days)} 天快照重建 + 全量入库")

    # ------------------------------------------------------------------ #
    # 板块成分（ths_member → dim_board_member）
    # ------------------------------------------------------------------ #
    def fetch_theme_members(self, date: str | None = None, *,
                            boards: list[str] | None = None,
                            max_age_days: float = 7.0, force: bool = False,
                            ingest_all: bool = False) -> None:
        """板块成分取数入库（按快照日）。

        Args:
            date: 快照戳 YYYYMMDD（= 本成分要服务的决策日）；默认最近板块榜日期。
            boards: 显式板块代码清单，覆盖 date 的板块清单。
            ingest_all: 不取数，仅把 raw 目录全量按各自戳灌入数仓。
        """
        if ingest_all:
            all_boards, stamps = [], set()
            for p in sorted(MEMBER_RAW.glob("ths_member_*.json")):
                if m := re.match(r"ths_member_(.+\.TI)_(\d{8})\.json$", p.name):
                    all_boards.append(m.group(1))
                    stamps.add(m.group(2))
            n = 0
            for stamp in sorted(stamps):
                n += ingest_members(sorted(set(all_boards)), stamp)
            print(f"[入库] dim_board_member <- {len(set(all_boards))} 板块 "
                  f"{len(stamps)} 个快照，共 {n} 行")
            return

        stamp = date or latest_rank_date()
        if boards:
            board_list = boards
        else:
            board_list = load_boards_from_rank(stamp)
            if not board_list:
                raise RuntimeError(f"fact_board_rank 无 {stamp} 板块数据")

        print(f"[计划] {len(board_list)} 个板块，快照戳 {stamp}")
        client = TushareClient()
        fetched, skipped = fetch_boards(client, board_list, stamp,
                                        max_age_days=max_age_days, force=force)
        client.write_call_log(f"theme_members_{stamp}")

        n = ingest_members(board_list, stamp)
        print(f"[入库] dim_board_member: {n} 行（戳 {stamp}；本次取数 {len(fetched)}，"
              f"缓存命中 {len(skipped)}）")
