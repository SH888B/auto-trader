# -*- coding: utf-8 -*-
"""Scout 数仓入库层（M2，对应 PRD §十）。

用法：
    uv run python scripts/scout/load_to_warehouse.py --all        # 全量入库（幂等）
    uv run python scripts/scout/load_to_warehouse.py --date 20261026   # 单日

行为：
- 建表契约来自 contracts/schema_v1.sql（文件即契约，改结构 bump 版本）；
- 幂等：入库前按主键日期 DELETE 再 INSERT，重复运行不产生重复行；
- 每行带 source_file（证据层文件）与 ingested_at，满足审计要求；
- trade_date 由 YYYYMMDD 解析为 DATE；原始字符串永远留在 data/raw/ 证据层。
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
from tushare_client import ROOT  # noqa: E402

DB_PATH = ROOT / "data" / "warehouse.duckdb"
SCHEMA_PATH = ROOT / "contracts" / "schema_v1.sql"
NOW = datetime.now()


def to_date(s: pd.Series) -> pd.Series:
    """YYYYMMDD(str/int) -> DATE。非法值置 NaT，由调用方决定是否报错。"""
    return pd.to_datetime(s.astype(str), format="%Y%m%d", errors="coerce").dt.date


def frame_from_raw(api: str, key: str) -> pd.DataFrame | None:
    """读取 raw JSON 为 DataFrame；不存在返回 None。"""
    path = ROOT / "data" / "raw" / api / f"{api}_{key}.json"
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload.get("items") or []
    if not items:
        return None
    return pd.DataFrame(items, columns=payload["fields"])


def rel(path: Path) -> str:
    """证据文件名（相对 data/raw/）。"""
    return str(path.relative_to(ROOT / "data" / "raw"))


# --------------------------------------------------------------------------- #
# 各表构建：返回 (表名, DataFrame)；DataFrame 列须与 schema 一致
# --------------------------------------------------------------------------- #
def build_fact_daily_bar(date: str) -> pd.DataFrame | None:
    df = frame_from_raw("daily", date)
    if df is None or not {"open", "high", "low"}.issubset(df.columns):
        return None
    df = df.copy()
    df["trade_date"] = to_date(df["trade_date"])
    df["source_file"] = f"daily/daily_{date}.json"
    df["ingested_at"] = NOW
    return df[["trade_date", "ts_code", "open", "high", "low", "close",
               "pre_close", "change", "pct_chg", "vol", "amount",
               "source_file", "ingested_at"]]


def build_index_rows() -> pd.DataFrame | None:
    """指数日线：raw 中 index_daily_<code>_<start>_<end>.json 全量载入（幂等去重）。"""
    frames = []
    for path in sorted((ROOT / "data" / "raw" / "index_daily").glob("index_daily_*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        items = payload.get("items") or []
        if not items:
            continue
        df = pd.DataFrame(items, columns=payload["fields"])
        df["trade_date"] = to_date(df["trade_date"])
        df["source_file"] = rel(path)
        df["ingested_at"] = NOW
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
    df["ingested_at"] = NOW
    return df[["trade_date", "ts_code", "up_limit", "down_limit", "source_file", "ingested_at"]]


def build_fact_limit_flag(date: str) -> pd.DataFrame | None:
    path = ROOT / "data" / "curated" / f"{date[:4]}-{date[4:6]}-{date[6:]}" / "limit_flags.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path, dtype={"ts_code": str})
    df["trade_date"] = to_date(pd.Series([date] * len(df)))
    df["source_file"] = f"curated/{path.parent.name}/limit_flags.csv"
    df["ingested_at"] = NOW
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
        df["ingested_at"] = NOW
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
    df["ingested_at"] = NOW
    return df[["trade_date", "ts_code", "name", "nums", "source_file", "ingested_at"]]


def build_fact_board_rank(date: str) -> pd.DataFrame | None:
    df = frame_from_raw("limit_cpt_list", date)
    if df is None:
        return None
    df = df.copy()
    df["trade_date"] = to_date(df["trade_date"])
    df["source_file"] = f"limit_cpt_list/limit_cpt_list_{date}.json"
    df["ingested_at"] = NOW
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
    df["ingested_at"] = NOW
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
        "source_file": f"curated/{path.parent.name}/breadth.csv", "ingested_at": NOW,
    }
    return pd.DataFrame([row])


def build_dim_stock() -> pd.DataFrame | None:
    df = frame_from_raw("stock_basic", "latest")
    if df is None:
        return None
    df = df.copy()
    df["list_date"] = to_date(df["list_date"])
    df["updated_at"] = NOW
    # stock_basic 接口不回传 list_status（它是请求参数），上市状态由请求条件保证为 L
    df["list_status"] = "L"
    keep = ["ts_code", "name", "industry", "market", "list_date", "list_status", "updated_at"]
    return df[[c for c in keep if c in df.columns]]


# --------------------------------------------------------------------------- #
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

DATE_TABLES = list(TABLE_BUILDERS)   # 按日期入库的表


def upsert(con: duckdb.DuckDBPyConnection, table: str, df: pd.DataFrame, dates: list[str]) -> int:
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Scout 数仓入库")
    parser.add_argument("--date", help="单日 YYYYMMDD")
    parser.add_argument("--all", action="store_true", help="全量（扫描 raw 全部日期）")
    args = parser.parse_args()

    # 收集日期清单
    if args.all:
        dates = sorted({re.search(r"(\d{8})", p.name).group(1)
                        for p in (ROOT / "data" / "raw" / "daily").glob("daily_*.json")
                        if re.search(r"(\d{8})", p.name)})
    elif args.date:
        dates = [args.date]
    else:
        raise SystemExit("需指定 --date 或 --all")

    con = duckdb.connect(str(DB_PATH))
    con.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    # 轻量迁移：老库补 v1.1 新增列（ADD COLUMN IF NOT EXISTS 幂等）
    for col, typ in [("float_mv", "DOUBLE"), ("turnover_ratio", "DOUBLE"), ("name", "VARCHAR")]:
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
    for table, builder in [("fact_daily_bar_idx", build_index_rows), ("dim_stock", build_dim_stock)]:
        df = builder()
        if df is None or df.empty:
            print(f"[跳过] {table}: 无数据")
            continue
        if table == "fact_daily_bar_idx":
            # 指数并入行情总表（同构）；删除条件按本次入库的指数代码集合，
            # 不可硬编码代码清单（新指数入库时会漏删导致重跑主键冲突），
            # 也不可加交易所后缀过滤（399001.SZ/399006.SZ 会被 '%.SH' 漏掉）
            dlist = ", ".join(f"DATE '{d}'" for d in sorted(set(df["trade_date"].astype(str))))
            codes = ", ".join(f"'{c}'" for c in sorted(set(df["ts_code"].astype(str))))
            con.execute(f"DELETE FROM fact_daily_bar WHERE trade_date IN ({dlist}) AND ts_code IN ({codes})")
            con.append("fact_daily_bar", df)
        else:
            con.execute("DELETE FROM dim_stock")
            con.append("dim_stock", df)
        total += len(df)
        print(f"[入库] {table}: {len(df)} 行")

    print(f"\n合计 {total} 行 -> {DB_PATH}")
    # DuckDB append 的注册对象可能是 Table 也可能是 View（版本差异），按类型清理
    obj_type = con.execute(
        "SELECT 'view' FROM duckdb_views WHERE view_name='__append_df'").fetchone()
    drop_sql = "DROP VIEW IF EXISTS __append_df" if obj_type else "DROP TABLE IF EXISTS __append_df"
    con.execute(drop_sql)
    print([t for t, in con.execute("SHOW TABLES").fetchall()])


if __name__ == "__main__":
    main()
