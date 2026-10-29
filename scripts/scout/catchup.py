# -*- coding: utf-8 -*-
"""Scout 历史回补工具（M3，对应 PRD M3 里程碑）。

用法：
    uv run python scripts/scout/catchup.py --check                 # 干跑：只列缺口
    uv run python scripts/scout/catchup.py --start 20261020 --end 20261026          # 补缺口
    uv run python scripts/scout/catchup.py --start 20261020 --end 20261026 --replay # 补完重建快照

设计要点：
- **交易日对齐**：以 trade_cal（is_open=1）为准，非交易日（如 2026-09-25 中秋）自动剔除，
  杜绝"假期假缺口"反复重试浪费转接资源；
- **缺口清单**：按"日期 × 接口"两维核对 data/raw/，只补缺的（幂等）；
- **重试策略**：继承 temp/daily_rescue.py 的"短超时高频重试抓转接好窗口"经验，
  日线类大 payload 45s × 多轮，打板类小 payload 标准退避；
- **--replay**：补完自动链式调用 build_snapshot → load_to_warehouse → select_stocks → finalize_snapshot，
  一条命令把历史日期重放成完整数据快照；Agent Digest 由 Curator 单独渲染。

注意：本脚本只负责"把缺的 raw 数据补齐"，不做任何清洗计算（那是下游管道的事）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tushare_client import ROOT, TushareClient  # noqa: E402

RAW = ROOT / "data" / "raw"

# 每日管道必需的接口及其 raw 文件命名规则（key 生成器）。
# daily 特殊：大 payload，需分页 + 抢救式重试。
# 注：抓取清单以 PRD/用户拍板为准（如 limit_step 退役则从此表移除）。
# limit_step（连板天梯）2026-10-28 用户拍板保留并入回补清单（fetch_daily 第 5 步已有）。
DAILY_APIS: list[tuple[str, dict]] = [
    ("stk_limit", lambda d: ({"trade_date": d}, d)),
    ("limit_list_d", lambda d: ({"trade_date": d, "limit_type": "U"}, f"{d}_up")),
    ("limit_list_d", lambda d: ({"trade_date": d, "limit_type": "D"}, f"{d}_down")),
    ("kpl_list", lambda d: ({"trade_date": d, "tag": "涨停"}, f"{d}_涨停")),
    ("limit_cpt_list", lambda d: ({"trade_date": d}, d)),
    ("limit_step", lambda d: ({"trade_date": d}, d)),
]


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
        d = client.call("trade_cal",
                        {"start_date": f"{year}0101", "end_date": f"{year}1231", "is_open": "1"},
                        timeout=60)
        body = d.get("data") or {}
        payload = {"fields": body.get("fields", []), "items": body.get("items", [])}
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        print(f"[trade_cal] 全年缓存落盘 {len(payload['items'])} 行 -> {cache}")
    idx = payload["fields"].index("cal_date")
    return sorted(str(r[idx]) for r in payload["items"] if start <= str(r[idx]) <= end)


def missing_files(date: str) -> list[tuple[str, str, dict]]:
    """返回该日缺失的 (api, key, params) 清单。
    kpl_list 空响应视为缺失（次日增强未到位，需重拉）；
    limit_list_d 空响应同样视为缺失（Tushare 原生接口当晚应到位，空=转接窗口问题）。"""
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
    from datetime import datetime, timedelta
    probe = (datetime.strptime(start, "%Y%m%d") - timedelta(days=10)).strftime("%Y%m%d")
    days = [d for d in load_trade_days(client, probe, start) if d < start]
    return days[-1] if days else None


def check(start: str, end: str) -> None:
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
    print(f"\n共 {total} 个缺口" + ("（需要先补 daily 才能算涨停反馈的日子， stk_limit 等也要齐）" if total else ""))


def fetch_daily_rescue(client: TushareClient, date: str, rounds: int = 8, timeout: int = 45) -> int:
    """日线抢救：短超时 × 多轮抓转接"好窗口"，两页分页（第二页 572 行全科创的历史教训）。

    来自 temp/daily_rescue.py 的验证过的策略，并入正式管道。
    返回落盘行数；全部轮次失败返回 0（调用方记录缺口）。
    """
    def page(offset: int) -> tuple[list, list] | None:
        params = {"trade_date": date}
        if offset:
            params["offset"] = str(offset)
        try:
            d = client.call("daily", params, timeout=timeout)
            body = d.get("data") or {}
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


def fetch_small(client: TushareClient, api: str, key: str, params: dict, timeout: int = 120) -> bool:
    """打板类小 payload 接口：标准退避（client 内置），成功落盘返回 True。"""
    try:
        d = client.call(api, params, timeout=timeout)
        body = d.get("data") or {}
        client.save_raw(api, key, {"fields": body.get("fields", []), "items": body.get("items", [])})
        print(f"  [{api}] {key}: {len(body.get('items', []))} 行")
        return True
    except Exception as exc:
        print(f"  [{api}] {key}: 失败 {str(exc)[:80]}")
        return False


def run_catchup(start: str, end: str) -> None:
    """补缺口主流程：逐交易日核对 → 先救 daily（大 payload）→ 再补打板类小接口。"""
    client = TushareClient(pace=1.2, max_retries=1, timeout=45)
    days = load_trade_days(client, start, end)
    print(f"区间 {start}~{end} 交易日 {len(days)} 天，开始核对缺口")
    # 前馈依赖：区间首日的"昨涨停反馈"需要前一交易日的 stk_limit（只补这一个接口）
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


def replay(start: str, end: str) -> None:
    """链式重建：对区间内每个有 raw 数据的交易日跑完整下游管道。"""
    import subprocess
    client = TushareClient(pace=1.0, max_retries=1, timeout=10)
    days = [d for d in load_trade_days(client, start, end)
            if (RAW / "daily" / f"daily_{d}.json").exists()]
    if not days:
        raise SystemExit("区间内无可用日线数据，无法重建")
    for d in days:
        print(f"\n===== 重建 {d} =====")
        # 顺序不能乱：select_stocks 从数仓读数，load 必须在它之前
        chain = [("build_snapshot.py", ["--date", d]),
                 ("load_to_warehouse.py", ["--date", d]),
                 ("select_stocks.py", ["--date", d]),
                 ("finalize_snapshot.py", ["--date", d])]
        for script, extra in chain:
            r = subprocess.run([sys.executable, str(ROOT / "scripts" / "scout" / script), *extra])
            if r.returncode != 0:
                raise SystemExit(f"{script} {' '.join(extra)} 失败，链式重建中断")
    print("\n===== 全量入库 =====")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "scout" / "load_to_warehouse.py"), "--all"])
    if r.returncode != 0:
        raise SystemExit("load_to_warehouse.py --all 失败")
    print(f"[replay] 完成：{len(days)} 天快照重建 + 全量入库")


def main() -> None:
    parser = argparse.ArgumentParser(description="Scout 历史回补")
    parser.add_argument("--start", required=True, help="起始日 YYYYMMDD（含）")
    parser.add_argument("--end", required=True, help="结束日 YYYYMMDD（含）")
    parser.add_argument("--check", action="store_true", help="只检查缺口，不抓取")
    parser.add_argument("--replay", action="store_true", help="补完后链式重建快照")
    args = parser.parse_args()

    if args.check:
        check(args.start, args.end)
        return

    run_catchup(args.start, args.end)
    if args.replay:
        replay(args.start, args.end)


if __name__ == "__main__":
    main()
