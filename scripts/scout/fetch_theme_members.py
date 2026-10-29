# -*- coding: utf-8 -*-
"""Scout 板块成分取数（ths_member → dim_board_member，按快照日存储）。

用法：
    uv run python scripts/scout/fetch_theme_members.py --date 20261028
    uv run python scripts/scout/fetch_theme_members.py --date 20261020      # 回测回放：盖历史快照戳
    uv run python scripts/scout/fetch_theme_members.py --boards 885517.TI,886069.TI --force
    uv run python scripts/scout/fetch_theme_members.py --ingest-all         # 仅把 raw 全量灌入数仓

行为与口径：
- **ths_member 只暴露"当前最新"成分**（is_new=Y），接口无法恢复成分的历史变更；
  回测精度 = 快照粒度。历史回放某天 D 前，用 --date D 跑一次，把当日所知成分
  盖上 D 的时间戳入库；查询侧取 trade_date <= as_of 的最新快照。
- 板块清单默认取 --date 当日 fact_board_rank 全部板块；--date 缺省为最近板块榜日期。
- raw 按日期命名落盘 data/raw/ths_member/ths_member_<board>_<YYYYMMDD>.json（只追加）；
  同戳且未过新鲜期（默认 7 天）的跳过；--force 强制重取并覆盖该戳快照。
- 取数完成后按 (trade_date, board_code) 幂等 upsert（先删后插）。

对应契约：contracts/schema_v1.sql ⑪（v1.2 起带 trade_date）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tushare_client import ROOT, TushareClient  # noqa: E402

DB_PATH = ROOT / "data" / "warehouse.duckdb"
SCHEMA_PATH = ROOT / "contracts" / "schema_v1.sql"
RAW_DIR = ROOT / "data" / "raw" / "ths_member"


def raw_path(board_code: str, stamp: str) -> Path:
    """stamp = YYYYMMDD 快照戳。"""
    return RAW_DIR / f"ths_member_{board_code}_{stamp}.json"


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
        raise SystemExit("fact_board_rank 为空，无法确定默认 --date")
    return row[0].strftime("%Y%m%d")


def load_boards_from_rank(date: str) -> list[str]:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        rows = con.execute(
            "SELECT DISTINCT board_code FROM fact_board_rank WHERE trade_date = ? ORDER BY board_code",
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
        path = raw_path(code, stamp)
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


def ingest(boards: list[str], stamp: str) -> int:
    """把 (板块, 快照戳) 的 raw 灌入 dim_board_member（幂等：先按戳删后插）。

    兼容未带日期的遗留 raw（ths_member_<code>.json）：按本次 stamp 入库。
    """
    stamp_date = datetime.strptime(stamp, "%Y%m%d").date()
    frames = []
    for code in boards:
        path = raw_path(code, stamp)
        legacy = RAW_DIR / f"ths_member_{code}.json"
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
    df = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["trade_date", "board_code", "con_code"])
    con = duckdb.connect(str(DB_PATH))
    try:
        con.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        codes = ", ".join(f"'{c}'" for c in sorted(set(df["board_code"])))
        con.execute(f"DELETE FROM dim_board_member WHERE trade_date = DATE '{stamp_date}' AND board_code IN ({codes})")
        con.append("dim_board_member", df)
    finally:
        con.close()
    return len(df)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scout：板块成分取数入库（按快照日）")
    parser.add_argument("--date", help="快照戳 YYYYMMDD（= 本成分要服务的决策日）；默认最近板块榜日期")
    parser.add_argument("--boards", help="显式板块代码清单（逗号分隔），覆盖 --date 的板块清单")
    parser.add_argument("--max-age-days", type=float, default=7, help="同戳 raw 缓存新鲜期（天），默认 7")
    parser.add_argument("--force", action="store_true", help="忽略缓存，重取并覆盖该戳快照")
    parser.add_argument("--ingest-all", action="store_true", help="不取数，仅把 raw 目录全量按各自戳灌入数仓")
    args = parser.parse_args()

    if args.ingest_all:
        boards, stamps = [], set()
        for p in sorted(RAW_DIR.glob("ths_member_*.json")):
            m = re.match(r"ths_member_(.+\.TI)_(\d{8})\.json$", p.name)
            if m:
                boards.append(m.group(1))
                stamps.add(m.group(2))
        n = 0
        for stamp in sorted(stamps):
            n += ingest(sorted(set(boards)), stamp)
        print(f"[入库] dim_board_member <- {len(set(boards))} 板块 {len(stamps)} 个快照，共 {n} 行")
        return

    stamp = args.date or latest_rank_date()
    if args.boards:
        boards = [b.strip() for b in args.boards.split(",") if b.strip()]
    else:
        boards = load_boards_from_rank(stamp)
        if not boards:
            raise SystemExit(f"fact_board_rank 无 {stamp} 板块数据")

    print(f"[计划] {len(boards)} 个板块，快照戳 {stamp}")
    client = TushareClient()
    fetched, skipped = fetch_boards(client, boards, stamp, max_age_days=args.max_age_days, force=args.force)
    client.write_call_log(f"theme_members_{stamp}")

    n = ingest(boards, stamp)
    print(f"[入库] dim_board_member: {n} 行（戳 {stamp}；本次取数 {len(fetched)}，缓存命中 {len(skipped)}）")


if __name__ == "__main__":
    main()
