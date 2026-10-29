# -*- coding: utf-8 -*-
"""Scout 取数层（M1，对应 PRD FR-1）。

用法：
    python scripts/scout/fetch_daily.py --dates 20261023 20261026      # 全量采集
    python scripts/scout/fetch_daily.py --dates 20261020 --light       # 轻量：仅日线+涨跌停价（历史回补）

行为：
- 校验每个日期是交易日（trade_cal，本地缓存优先）；
- 按接口路由表调用并落盘 data/raw/<api>/，幂等（已存在跳过，--force 重写）；
- kpl_list 数据源次日 8:30 才更新，当日收盘后立刻运行可能拿到空/旧数据，属正常；
- 结束时输出调用统计并写 temp/call_log_<首个日期>.json。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tushare_client import ROOT, TushareClient  # noqa: E402

# 全量采集的接口清单：(api, 构造参数的函数, name_key, 分页?)
# name_key 用于落盘文件名；历史类数据（指数区间、主档、板块清单）单独缓存，不进每日清单。
FULL_SPECS = [
    ("daily",        lambda d: {"trade_date": d}, d := "daily", True),   # 占位，见下方 fetch_full_date
]


def _norm_items(data: dict) -> dict:
    """把官方响应整理成统一落盘格式：{fields, items, fetched_at?}。"""
    body = data.get("data") or {}
    return {"fields": body.get("fields", []), "items": body.get("items", [])}


def ensure_trade_cal(client: TushareClient, force: bool = False) -> list[str]:
    """确保交易日历缓存存在（2025-01 起两年），返回全部交易日列表 YYYYMMDD。"""
    cached = client.load_raw("trade_cal", "20250101_20261231")
    if cached is None or force:
        data = client.call("trade_cal", {"exchange": "SSE", "start_date": "20250101", "end_date": "20261231"})
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
            fields, items = client.call_paginated("stock_basic", {"list_status": "L"}, page_size=4000)
            client.save_raw("stock_basic", "latest", {"fields": fields, "items": items}, force=True)
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
    """指数日线区间缓存：三大指数各拉取最近 days 个自然日，存 index_daily_<code>_range。"""
    from datetime import datetime, timedelta
    start = (datetime.strptime(end_date, "%Y%m%d") - timedelta(days=days)).strftime("%Y%m%d")
    for code in ["000001.SH", "399001.SZ", "399006.SZ",
                 "399317.SZ",   # 国证A指：全A代表（E003，中证全指 000985 无数据，用户批准替代）
                 "000688.SH",   # 科创50：科技/成长/风偏
                 "399986.SZ",   # 中证银行：风偏降低（哑铃端）
                 "932000.CSI"]:  # 中证2000：投机/情绪先行指标（微盘股替代）
        if client.load_raw("index_daily", f"{code}_{start}_{end_date}") is None:
            data = client.call("index_daily", {"ts_code": code, "start_date": start, "end_date": end_date})
            client.save_raw("index_daily", f"{code}_{start}_{end_date}", _norm_items(data), force=True)


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Scout 取数层")
    parser.add_argument("--dates", nargs="+", required=True, help="交易日 YYYYMMDD，可多个")
    parser.add_argument("--light", action="store_true", help="轻量模式：仅日线+涨跌停价")
    parser.add_argument("--force", action="store_true", help="已落盘的数据也重新拉取")
    args = parser.parse_args()

    client = TushareClient(pace=1.5, max_retries=5, timeout=180)
    trading_days = ensure_trade_cal(client)
    for d in args.dates:
        if d not in trading_days:
            print(f"[跳过] {d} 不是交易日")
            continue

    if not args.light:
        ensure_weekly_caches(client)
        ensure_index_history(client, max(args.dates))

    t0 = time.monotonic()
    for d in args.dates:
        if d not in trading_days:
            continue
        try:
            stats = fetch_light_date(client, d) if args.light else fetch_full_date(client, d)
            print(f"[完成] {d}: " + ", ".join(f"{k}={v}" for k, v in stats.items()))
        except Exception as exc:  # 单日期失败不阻塞其余日期，但报告缺失
            print(f"[失败] {d}: {exc}")

    log = client.write_call_log(args.dates[0])
    n_calls = len(client.call_log)
    errors = sum(1 for c in client.call_log if "error" in c)
    print(f"\n调用 {n_calls} 次（含重试），失败 {errors} 次，耗时 {time.monotonic() - t0:.0f}s")
    print(f"调用日志: {log}")


if __name__ == "__main__":
    main()
