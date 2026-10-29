# -*- coding: utf-8 -*-
"""Execute an allow-listed, point-in-time query and freeze request/response artifacts.

Example:
    uv run python scripts/curator/query_data.py --request temp/query.yaml
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs" / "queries" / "templates.yaml"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def ref(path: Path) -> dict:
    return {"path": path.relative_to(ROOT).as_posix(), "sha256": sha256(path)}


def parse_date(value: str | date) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def safe_output(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT):
        raise ValueError("输出必须位于 workspace 内")
    return resolved


def daily_bars(con, request: dict) -> tuple[pd.DataFrame, dict]:
    p = request.get("params") or {}
    codes = p.get("ts_codes") or []
    if not codes or not all(isinstance(c, str) for c in codes):
        raise ValueError("get_daily_bars 需要非空 ts_codes 数组")
    as_of = parse_date(request["as_of"])
    lookback = int(p.get("lookback_days", 60))
    if not 1 <= lookback <= 3660:
        raise ValueError("lookback_days 须在 1–3660 之间")
    start = as_of - timedelta(days=lookback)
    placeholders = ",".join(["?"] * len(codes))
    df = con.execute(f"""
        SELECT trade_date, ts_code, open, high, low, close, pre_close,
               change, pct_chg, vol, amount
        FROM fact_daily_bar
        WHERE ts_code IN ({placeholders})
          AND trade_date BETWEEN ? AND ?
        ORDER BY ts_code, trade_date
    """, [*codes, start, as_of]).fetchdf()
    return df, {"lookback_days": lookback, "start_date": str(start)}


def theme_members(con, request: dict) -> tuple[pd.DataFrame, dict]:
    """Return event-driven theme candidates; full THS membership is not yet loaded."""
    p = request.get("params") or {}
    theme = str(p.get("theme_id") or p.get("theme") or "").strip()
    if not theme:
        raise ValueError("get_theme_members 需要 theme_id/theme")
    as_of = parse_date(request["as_of"])
    lookback = int(p.get("lookback_days", 20))
    start = as_of - timedelta(days=lookback)
    df = con.execute("""
        SELECT ts_code, MAX(trade_date) AS last_event_date,
               STRING_AGG(DISTINCT theme, '、') AS matched_themes,
               'kpl_limit_event' AS matched_by
        FROM fact_kpl_list
        WHERE trade_date BETWEEN ? AND ? AND theme ILIKE ?
        GROUP BY ts_code
        ORDER BY last_event_date DESC, ts_code
    """, [start, as_of, f"%{theme}%"]).fetchdf()
    if not df.empty:
        names = con.execute("SELECT ts_code, name FROM dim_stock").fetchdf()
        df = df.merge(names, on="ts_code", how="left")
    metadata = {
        "coverage": "limit_event_candidates_only",
        "limitation": "当前未装载完整同花顺板块成员；结果仅是回看窗口内的开盘啦涨跌停事件候选，不能声称全覆盖",
        "lookback_days": lookback,
        "start_date": str(start),
    }
    return df, metadata


def snapshot_table(con, request: dict) -> tuple[pd.DataFrame, dict]:
    del con
    p = request.get("params") or {}
    table = str(p.get("table") or "")
    allowed = set((load_yaml(CONFIG).get("allowed_snapshot_tables") or []))
    if table not in allowed:
        raise ValueError(f"snapshot table 未允许：{table}")
    as_of = request["as_of"]
    path = ROOT / "data" / "snapshots" / as_of / "derived" / f"{table}.csv"
    if not path.exists():
        raise FileNotFoundError(f"快照表不存在：{path}")
    df = pd.read_csv(path, encoding="utf-8-sig")
    columns = p.get("columns") or []
    if columns:
        missing = [c for c in columns if c not in df.columns]
        if missing:
            raise ValueError(f"字段不存在：{missing}")
        df = df.loc[:, columns]
    limit = int(p.get("limit", 100))
    if not 1 <= limit <= 10000:
        raise ValueError("limit 须在 1–10000 之间")
    return df.head(limit), {"source": ref(path), "limit": limit, "source_rows": len(df)}


def board_momentum(con, request: dict) -> tuple[pd.DataFrame, dict]:
    """Judge 预判的明日首分板块 → 全量成分股按两日涨幅排序（Trader 择股主输入）。"""
    p = request.get("params") or {}
    boards = p.get("board_codes") or []
    if not boards or not all(isinstance(b, str) for b in boards):
        raise ValueError("get_board_momentum 需要非空 board_codes 数组")
    as_of = parse_date(request["as_of"])
    top_n = int(p.get("top_n", 20))
    if not 1 <= top_n <= 200:
        raise ValueError("top_n 须在 1–200 之间")
    dates_df = con.execute("""
        SELECT DISTINCT trade_date FROM fact_daily_bar
        WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT 5
    """, [as_of]).fetchdf()
    if len(dates_df) < 2:
        raise ValueError("as_of 前不足两个交易日，无法计算两日涨幅")
    d0, d1 = dates_df.iloc[0]["trade_date"], dates_df.iloc[1]["trade_date"]
    d4 = dates_df.iloc[4]["trade_date"] if len(dates_df) >= 5 else None
    placeholders = ",".join(["?"] * len(boards))
    gain5 = ("ROUND((b0.close / b4.pre_close - 1) * 100, 2)" if d4 is not None
             else "CAST(NULL AS DOUBLE)")
    df = con.execute(f"""
        WITH snap AS (
            SELECT board_code, MAX(trade_date) AS md
            FROM dim_board_member
            WHERE trade_date <= ? AND board_code IN ({placeholders})
            GROUP BY board_code
        ),
        members AS (
            SELECT m.board_code, m.con_code, m.con_name, m.trade_date AS member_snapshot_date
            FROM dim_board_member m
            JOIN snap s ON s.board_code = m.board_code AND m.trade_date = s.md
            WHERE m.is_new = 'Y'
        ),
        bn AS (
            SELECT board_code, MAX(board_name) AS board_name
            FROM fact_board_rank WHERE trade_date <= ? GROUP BY board_code
        )
        SELECT m.board_code, bn.board_name, m.con_code, m.con_name,
               ROUND((b0.close / b1.pre_close - 1) * 100, 2) AS gain_2d_pct,
               {gain5} AS gain_5d_pct,
               ROUND(b1.pct_chg, 2) AS d1_pct_chg,
               ROUND(b0.pct_chg, 2) AS d0_pct_chg,
               b0.close, ROUND(b0.amount / 1e5, 2) AS amount_yi,
               m.member_snapshot_date
        FROM members m
        JOIN fact_daily_bar b0 ON b0.ts_code = m.con_code AND b0.trade_date = ?
        JOIN fact_daily_bar b1 ON b1.ts_code = m.con_code AND b1.trade_date = ?
        LEFT JOIN fact_daily_bar b4 ON b4.ts_code = m.con_code AND b4.trade_date = ?
        LEFT JOIN bn ON bn.board_code = m.board_code
    """, [as_of, *boards, as_of, d0, d1, d4 or d0]).fetchdf()
    boards_missing = boards
    member_snap = None
    if not df.empty:
        df = df.sort_values(["board_code", "gain_2d_pct"], ascending=[True, False])
        df["rank_in_board"] = df.groupby("board_code").cumcount() + 1
        df = df[df["rank_in_board"] <= top_n]
        member_snap = str(df["member_snapshot_date"].min().date())
        df = df[["board_code", "board_name", "con_code", "con_name", "rank_in_board",
                 "gain_2d_pct", "gain_5d_pct", "d1_pct_chg", "d0_pct_chg", "close", "amount_yi"]]
        boards_missing = [b for b in boards if b not in set(df["board_code"])]
    metadata = {
        "d0": str(d0), "d1": str(d1), "top_n": top_n,
        "member_snapshot_date": member_snap,
        "coverage": "dim_board_member 全量成分（is_new=Y），按两日涨幅取前 N",
        "limitation": "成分为 trade_date<=as_of 的最新快照（接口无历史变更，回测精度=快照粒度）；停牌、上市不足两日无完整两根K线者被剔除；gain_5d_pct 不足五个交易日者为空",
        "boards_without_members": boards_missing,
    }
    return df, metadata


HANDLERS = {
    "get_daily_bars": daily_bars,
    "get_theme_members": theme_members,
    "get_snapshot_table": snapshot_table,
    "get_board_momentum": board_momentum,
}

def main() -> None:
    parser = argparse.ArgumentParser(description="Curator 受控时点查询")
    parser.add_argument("--request", required=True, help="查询请求 YAML")
    parser.add_argument("--output-dir", help="查询产物目录；默认使用请求中的 output_dir 或 live Run")
    args = parser.parse_args()
    request_path = Path(args.request).resolve()
    request = load_yaml(request_path)
    query = request.get("query")
    as_of = request.get("as_of")
    if query not in HANDLERS:
        raise SystemExit(f"不允许的 query：{query}")
    try:
        as_of = parse_date(as_of).isoformat()
        request["as_of"] = as_of
    except (TypeError, ValueError):
        raise SystemExit("as_of 必须为 YYYY-MM-DD")

    config = load_yaml(CONFIG)
    allowed = set(config.get("allowed_queries") or [])
    if query not in allowed:
        raise SystemExit(f"query template 未启用：{query}")

    query_id = hashlib.sha256(
        json.dumps({"query": query, "as_of": as_of, "params": request.get("params") or {}},
                   ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    output_arg = args.output_dir or request.get("output_dir")
    if output_arg:
        out_dir = safe_output(Path(output_arg))
    else:
        out_dir = ROOT / "runs" / "live" / as_of / request.get("module", "trader") / "queries" / f"{query}-{query_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    frozen_request = dict(request)
    frozen_request.pop("output_dir", None)
    request_out = out_dir / "request.yaml"
    request_out.write_text(yaml.safe_dump(frozen_request, allow_unicode=True, sort_keys=False), encoding="utf-8")

    db = ROOT / "data" / "warehouse.duckdb"
    if not db.exists():
        raise SystemExit(f"数仓不存在：{db}")
    con = duckdb.connect(str(db), read_only=True)
    started = datetime.now().astimezone()
    try:
        df, metadata = HANDLERS[query](con, request)
    finally:
        con.close()
    response_path = out_dir / "response.csv"
    df.to_csv(response_path, index=False, encoding="utf-8-sig")
    response_meta = {
        "query_id": query_id,
        "query": query,
        "as_of": as_of,
        "started_at": started.isoformat(timespec="seconds"),
        "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "rows": len(df),
        "request": ref(request_out),
        "response": ref(response_path),
        **metadata,
    }
    meta_path = out_dir / "response_metadata.yaml"
    meta_path.write_text(yaml.safe_dump(response_meta, allow_unicode=True, sort_keys=False), encoding="utf-8")

    module_dir = out_dir.parent.parent if out_dir.parent.name == "queries" else out_dir
    manifest_path = module_dir / "input_manifest.yaml"
    manifest = load_yaml(manifest_path) if manifest_path.exists() else {
        "manifest_version": "im1.0", "run_id": request.get("run_id"),
        "module": request.get("module", "trader"), "as_of": as_of, "inputs": [], "queries": [],
    }
    manifest.setdefault("queries", []).append(ref(meta_path))
    manifest_path.write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"[query] {query} rows={len(df)} as_of={as_of}")
    print(f"[response] {response_path}")
    print(f"[manifest] {manifest_path}")


if __name__ == "__main__":
    main()
