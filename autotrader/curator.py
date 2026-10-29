"""Curator：上下文服务（Digest 渲染、受控查询、Bundle 冻结、watchlist 应用）。

由 scripts/curator/ 4 个脚本迁入（DESIGN-V4 §六-5），逻辑逐行等价。
有意的变更仅限：模块常量统一来自 core.WORKSPACE_ROOT；argparse main() →
class Curator 方法；build_bundle 的 scripts_sha256 改指 autotrader/ 实现文件
（字段名保持 bundle3.0 兼容）；数据库连接统一 try/finally 关闭。

Curator 纪律（ARCHITECTURE-V3 §四）：只按模板和参数确定性渲染，
不做自由摘要、不判断行情重要性、不静默删除缺口、不拼入未来数据。
"""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import yaml
from jsonschema import Draft7Validator

from .core import (
    WORKSPACE_ROOT,
    StateStore,
    Workspace,
    WorkspaceError,
    load_yaml,
    sha256_file,
)

ROOT = WORKSPACE_ROOT
DB_PATH = ROOT / "data" / "warehouse.duckdb"
DICT_PATH = ROOT / "configs" / "digest" / "column_dictionary.yaml"
QUERY_CONFIG = ROOT / "configs" / "queries" / "templates.yaml"
REVIEW_SCHEMA = ROOT / "contracts" / "review.schema.json"


# --------------------------------------------------------------------------- #
# Digest 渲染 helpers（原 render_digest.py）
# --------------------------------------------------------------------------- #
def load_dictionary() -> tuple[dict, set[str]]:
    dictionary = load_yaml(DICT_PATH)
    return dictionary.get("columns", {}), set(dictionary.get("exclude_columns", []))


def fmt(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value).replace("|", "\\|").replace("\n", " ")


def fmt_cell(value: Any, spec: dict | None) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, (int, float)) and spec:
        v = float(value)
        div = {"yi5": 1e5, "yi8": 1e8}.get(str(spec.get("fmt")), 1)
        ndec = {"price2": 2, "pct2": 2, "yi5": 2, "yi8": 2, "int0": 0}.get(
            str(spec.get("fmt")), 2)
        return f"{v / div:.{ndec}f}"
    return fmt(value)


def markdown_table(df: pd.DataFrame, columns: list[str], limit: int,
                   dictionary: dict, exclude: set[str]) -> str:
    if df.empty:
        return "（无数据）"
    cols = [c for c in columns if c in df.columns] if columns else list(df.columns)
    cols = [c for c in cols if c not in exclude]
    if not cols:
        return "（配置列均不存在）"
    view = df.loc[:, cols].head(limit)
    header = [dictionary.get(c, {}).get("zh", c) for c in cols]
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join(["---"] * len(cols)) + "|",
    ]
    for _, row in view.iterrows():
        cells = " | ".join(fmt_cell(row[c], dictionary.get(c)) for c in cols)
        lines.append(f"| {cells} |")
    return "\n".join(lines)


WATCHLIST_COLUMNS = ["ts_code", "name", "open", "high", "low", "close",
                     "pre_close", "pct_chg", "vol", "amount", "role", "reason"]


def watchlist_frame(state_path: Path, trade_date: str) -> pd.DataFrame:
    """把一份冻结的 watchlist 状态 join 当日行情，渲染成原"关注列表"表。

    watchlist 是 state，不是行情：这里只做确定性 join，不做任何筛选判断。
    """
    state = load_yaml(state_path)
    stocks = [s for s in (state.get("stocks") or [])
              if str(s.get("status", "关注")) == "关注" and s.get("ts_code")]
    if not stocks:
        return pd.DataFrame(columns=WATCHLIST_COLUMNS)
    codes = [str(s["ts_code"]) for s in stocks]
    placeholders = ",".join(["?"] * len(codes))
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        bars = con.execute(
            f"""SELECT ts_code, open, high, low, close, pre_close,
                       pct_chg, vol, amount
                FROM fact_daily_bar
                WHERE trade_date = ? AND ts_code IN ({placeholders})""",
            [trade_date, *codes]).fetchdf()
        names = con.execute(
            f"SELECT ts_code, name FROM dim_stock WHERE ts_code IN ({placeholders})",
            codes).fetchdf()
    finally:
        con.close()
    meta = pd.DataFrame([{"ts_code": s["ts_code"], "name": s.get("name"),
                          "role": s.get("role"), "reason": s.get("reason")}
                         for s in stocks])
    df = meta.merge(bars, on="ts_code", how="left").merge(
        names, on="ts_code", how="left", suffixes=("", "_stock"))
    df["name"] = df["name"].where(df["name"].notna(), df.get("name_stock"))
    return df.loc[:, WATCHLIST_COLUMNS]


# --------------------------------------------------------------------------- #
# 受控查询 helpers（原 query_data.py）
# --------------------------------------------------------------------------- #
def parse_date(value: str | date) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def daily_bars(con: duckdb.DuckDBPyConnection,
               request: dict) -> tuple[pd.DataFrame, dict]:
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


def theme_members(con: duckdb.DuckDBPyConnection,
                  request: dict) -> tuple[pd.DataFrame, dict]:
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
        "limitation": "当前未装载完整同花顺板块成员；结果仅是回看窗口内的"
                      "开盘啦涨跌停事件候选，不能声称全覆盖",
        "lookback_days": lookback,
        "start_date": str(start),
    }
    return df, metadata


def snapshot_table(con: duckdb.DuckDBPyConnection,
                   request: dict) -> tuple[pd.DataFrame, dict]:
    del con  # 快照表走文件，不用数仓连接
    p = request.get("params") or {}
    table = str(p.get("table") or "")
    allowed = set((load_yaml(QUERY_CONFIG).get("allowed_snapshot_tables") or []))
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
    ws = Workspace()
    return df.head(limit), {"source": ws.artifact_ref(path), "limit": limit,
                            "source_rows": len(df)}


def board_momentum(con: duckdb.DuckDBPyConnection,
                   request: dict) -> tuple[pd.DataFrame, dict]:
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
                 "gain_2d_pct", "gain_5d_pct", "d1_pct_chg", "d0_pct_chg",
                 "close", "amount_yi"]]
        boards_missing = [b for b in boards if b not in set(df["board_code"])]
    metadata = {
        "d0": str(d0), "d1": str(d1), "top_n": top_n,
        "member_snapshot_date": member_snap,
        "coverage": "dim_board_member 全量成分（is_new=Y），按两日涨幅取前 N",
        "limitation": "成分为 trade_date<=as_of 的最新快照（接口无历史变更，回测精度="
                      "快照粒度）；停牌、上市不足两日无完整两根K线者被剔除；"
                      "gain_5d_pct 不足五个交易日者为空",
        "boards_without_members": boards_missing,
    }
    return df, metadata


QUERY_HANDLERS = {
    "get_daily_bars": daily_bars,
    "get_theme_members": theme_members,
    "get_snapshot_table": snapshot_table,
    "get_board_momentum": board_momentum,
}


def code_hash() -> str:
    """Curator/引擎实现文件的组合 hash（bundle 工程锁用，保持字段名兼容）。"""
    digest = hashlib.sha256()
    for path in sorted((ROOT / "autotrader").glob("*.py")):
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# Curator 服务：原 4 个脚本的 main() 收为方法
# --------------------------------------------------------------------------- #
class Curator:
    """Curator 上下文服务：Agent 视图渲染、受控时点查询、实验 Bundle 冻结、watchlist 应用。

    所有方法确定性执行：同输入同输出；失败抛异常，不静默吞缺口。
    """

    def __init__(self, workspace: Workspace | None = None) -> None:
        self.ws = workspace or Workspace()

    def _rel(self, path: Path) -> str:
        """工作区相对路径（manifest / digest 展示用）。"""
        return path.resolve().relative_to(self.ws.root.resolve()).as_posix()

    # ------------------------------------------------------------------ #
    # Digest 渲染（原 render_digest.py）
    # ------------------------------------------------------------------ #
    def render_digest(self, date: str, *, view: str = "configs/digest/judge_default.yaml",
                      data: str = "configs/data/data_recipe.yaml",
                      output: str | None = None, run_id: str | None = None,
                      module: str = "judge", state_root: str = "state") -> Path:
        """从标准快照按 View Recipe 渲染确定性 Digest，并写 input_manifest。

        Args:
            date: 决策时点 YYYYMMDD。
            view: View Recipe 路径（display_n/sections 配置）。
            data: Data Recipe 路径。
            output: Digest 输出路径；默认 runs/live/<date>/<module>/digest.md。
            module: judge / trader。
            state_root: watchlist/portfolio 状态根目录（实验传实验私有 state）。
        """
        date_dash = f"{date[:4]}-{date[4:6]}-{date[6:]}"
        view_path = self.ws.path(view)
        data_path = self.ws.path(data)
        view_cfg = load_yaml(view_path)
        data_recipe = load_yaml(data_path)
        snap_dir = ROOT / "data" / "snapshots" / date_dash
        snapshot_path = snap_dir / "snapshot.yaml"
        if not snapshot_path.exists():
            raise FileNotFoundError(f"数据快照不存在：{snapshot_path}")
        snapshot = load_yaml(snapshot_path)
        dictionary, exclude = load_dictionary()

        # P0-1 硬校验：时点隔离从约定升级为机制
        data_as_of = str(snapshot.get("data_as_of") or "")
        if not data_as_of:
            raise WorkspaceError(f"snapshot 缺少 data_as_of（P0-1 硬校验拒绝）：{snapshot_path}")
        if data_as_of < date_dash:
            raise WorkspaceError(
                f"snapshot 数据未覆盖请求日期：data_as_of={data_as_of} < as_of={date_dash}")
        derived_ver = snapshot.get("derived_code_version")
        if not derived_ver:
            raise WorkspaceError(
                f"snapshot 缺少 derived_code_version（P0-1 硬校验拒绝）：{snapshot_path}")

        out_path = self.ws.path(output) if output else (
            ROOT / "runs" / "live" / date_dash / module / "digest.md")
        out_path.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            f"# {module.title()} Digest —— {date_dash}",
            "",
            f"> as_of: {date_dash} ｜ view: {view_cfg.get('version', '?')} ｜ "
            f"data: {data_recipe.get('version', '?')} ｜ 仅含决策时点允许数据",
            "",
        ]
        inputs = [self.ws.artifact_ref(snapshot_path), self.ws.artifact_ref(view_path),
                  self.ws.artifact_ref(data_path), self.ws.artifact_ref(DICT_PATH)]
        gaps = list(snapshot.get("gaps") or [])
        meta: dict[str, str] = {}

        def note_meta(df: pd.DataFrame) -> None:
            for col in ("selectors_version", "trade_date"):
                if col in df.columns:
                    values = {str(v) for v in df[col].dropna().unique()}
                    if len(values) == 1:
                        meta[col] = values.pop()

        for section in view_cfg.get("sections", []):
            title = section["title"]
            lines.extend([f"## {title}", ""])
            if section.get("note"):
                for note_line in str(section["note"]).strip().splitlines():
                    lines.append(f"> {note_line.strip()}")
                lines.append("")
            if section.get("type") == "breadth":
                path = ROOT / "data" / "curated" / date_dash / "breadth.csv"
                if path.exists():
                    df = pd.read_csv(path, encoding="utf-8-sig")
                    note_meta(df)
                    lines.append(markdown_table(df, section.get("columns", []),
                                                int(section.get("display_n", 100)),
                                                dictionary, exclude))
                    inputs.append(self.ws.artifact_ref(path))
                else:
                    lines.append("（breadth 缺失）")
                    gaps.append(f"{title}: breadth.csv 缺失")
                lines.append("")
                continue

            if section.get("type") == "watchlist":
                source = str(section.get("source", "judge"))
                history = f"{state_root}/watchlist/{source}/history"
                current = f"{state_root}/watchlist/{source}/current.yaml"
                entry = StateStore(self.ws).as_of_path(
                    current, date_dash, history, strict="before")
                if entry is None:
                    raise WorkspaceError(
                        f"{title}: 缺 {date_dash} 之前的 {source} watchlist 快照"
                        f"（{history}）；watchlist 是每日状态，不能回退 current")
                df = watchlist_frame(entry, date_dash)
                display_n = int(section.get("display_n", 50))
                lines.append(f"输入：`{self._rel(entry)}`（严格早于 {date_dash}）；"
                             f"关注中 {len(df)} 只。")
                lines.append("")
                lines.append(markdown_table(df, section.get("columns", []), display_n,
                                            dictionary, exclude))
                lines.append("")
                inputs.append(self.ws.artifact_ref(entry))
                continue

            table = section["table"]
            path = snap_dir / "derived" / f"{table}.csv"
            if not path.exists():
                lines.append(f"（{table} 缺失）")
                lines.append("")
                gaps.append(f"{title}: derived/{table}.csv 缺失")
                continue
            df = pd.read_csv(path, encoding="utf-8-sig")
            note_meta(df)
            display_n = int(section.get("display_n", 20))
            lines.append(f"标准表共 {len(df)} 行；本视图展开前 {min(display_n, len(df))} 行。")
            lines.append("")
            lines.append(markdown_table(df, section.get("columns", []), display_n,
                                        dictionary, exclude))
            lines.append("")
            inputs.append(self.ws.artifact_ref(path))

        if meta:
            lines[2] += " ｜ " + " ｜ ".join(f"{k}: {v}" for k, v in sorted(meta.items()))
        lines[2] += f" ｜ derived_code_version: {derived_ver}"

        lines.extend(["## 数据缺口", ""])
        unique_gaps = list(dict.fromkeys(str(g) for g in gaps if str(g) != "无"))
        lines.extend([f"- {g}" for g in unique_gaps] or ["- 无"])
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        manifest = {
            "manifest_version": "im1.0",
            "run_id": run_id or f"live-{date}-{module}",
            "module": module,
            "as_of": date_dash,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "data_snapshot": self.ws.artifact_ref(snapshot_path),
            "data_recipe": self.ws.artifact_ref(data_path),
            "view_recipe": self.ws.artifact_ref(view_path),
            "digest": self.ws.artifact_ref(out_path),
            "inputs": inputs,
            "queries": [],
            "gaps": unique_gaps,
        }
        manifest_path = out_path.parent / "input_manifest.yaml"
        manifest_path.write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
                                 encoding="utf-8")
        print(f"[digest] {out_path}")
        print(f"[manifest] {manifest_path}")
        return out_path

    # ------------------------------------------------------------------ #
    # 受控时点查询（原 query_data.py）
    # ------------------------------------------------------------------ #
    def query(self, request: dict, *, output_dir: str | None = None) -> Path:
        """执行白名单内的 as_of 受控查询，冻结请求/响应/元数据并登记 input_manifest。

        Args:
            request: 查询请求 dict，至少含 query、as_of；可含 params/module/run_id/output_dir。
            output_dir: 产物目录；默认使用请求中的 output_dir 或 live Run 约定路径。

        Returns:
            响应 CSV 路径。
        """
        query_name = request.get("query")
        as_of = request.get("as_of")
        if query_name not in QUERY_HANDLERS:
            raise WorkspaceError(f"不允许的 query：{query_name}")
        try:
            as_of = parse_date(as_of).isoformat()
            request["as_of"] = as_of
        except (TypeError, ValueError) as exc:
            raise WorkspaceError("as_of 必须为 YYYY-MM-DD") from exc

        config = load_yaml(QUERY_CONFIG)
        allowed = set(config.get("allowed_queries") or [])
        if query_name not in allowed:
            raise WorkspaceError(f"query template 未启用：{query_name}")

        query_id = hashlib.sha256(
            json.dumps({"query": query_name, "as_of": as_of,
                        "params": request.get("params") or {}},
                       ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:12]
        output_arg = output_dir or request.get("output_dir")
        if output_arg:
            out_dir = self.ws.path(output_arg)
        else:
            out_dir = (ROOT / "runs" / "live" / as_of
                       / request.get("module", "trader") / "queries"
                       / f"{query_name}-{query_id}")
        out_dir.mkdir(parents=True, exist_ok=True)
        frozen_request = dict(request)
        frozen_request.pop("output_dir", None)
        request_out = out_dir / "request.yaml"
        request_out.write_text(yaml.safe_dump(frozen_request, allow_unicode=True,
                                              sort_keys=False), encoding="utf-8")

        if not DB_PATH.exists():
            raise FileNotFoundError(f"数仓不存在：{DB_PATH}")
        con = duckdb.connect(str(DB_PATH), read_only=True)
        started = datetime.now().astimezone()
        try:
            df, metadata = QUERY_HANDLERS[query_name](con, request)
        finally:
            con.close()
        response_path = out_dir / "response.csv"
        df.to_csv(response_path, index=False, encoding="utf-8-sig")
        response_meta = {
            "query_id": query_id,
            "query": query_name,
            "as_of": as_of,
            "started_at": started.isoformat(timespec="seconds"),
            "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "rows": len(df),
            "request": self.ws.artifact_ref(request_out),
            "response": self.ws.artifact_ref(response_path),
            **metadata,
        }
        meta_path = out_dir / "response_metadata.yaml"
        meta_path.write_text(yaml.safe_dump(response_meta, allow_unicode=True,
                                            sort_keys=False), encoding="utf-8")

        module_dir = out_dir.parent.parent if out_dir.parent.name == "queries" else out_dir
        manifest_path = module_dir / "input_manifest.yaml"
        manifest = load_yaml(manifest_path) if manifest_path.exists() else {
            "manifest_version": "im1.0", "run_id": request.get("run_id"),
            "module": request.get("module", "trader"), "as_of": as_of,
            "inputs": [], "queries": [],
        }
        manifest.setdefault("queries", []).append(self.ws.artifact_ref(meta_path))
        manifest_path.write_text(yaml.safe_dump(manifest, allow_unicode=True,
                                                sort_keys=False), encoding="utf-8")
        print(f"[query] {query_name} rows={len(df)} as_of={as_of}")
        print(f"[response] {response_path}")
        print(f"[manifest] {manifest_path}")
        return response_path

    # ------------------------------------------------------------------ #
    # Bundle 冻结（原 build_bundle.py）
    # ------------------------------------------------------------------ #
    def build_bundle(self, experiment: str) -> Path:
        """解析 experiment.yaml，冻结小型文本资产并写 hash 锁定清单。

        Raises:
            WorkspaceError: Bundle 已冻结（拒绝覆盖）或资产缺失/越界。
        """
        exp_dir = self.ws.path(experiment)
        experiment_path = exp_dir / "experiment.yaml"
        config = load_yaml(experiment_path)
        lock_path = exp_dir / "bundle" / "manifest.lock.yaml"
        if lock_path.exists():
            raise WorkspaceError(f"Bundle 已冻结，拒绝覆盖：{lock_path}")

        copied: dict[str, dict] = {}

        def freeze(rel: str, category: str = "configs") -> dict:
            if not rel:
                return {}
            src = self.ws.path(rel)
            if not src.is_file():
                raise WorkspaceError(f"冻结资产不存在或越界：{rel}")
            key = src.relative_to(self.ws.root.resolve()).as_posix()
            if key not in copied:
                dst = exp_dir / "bundle" / category / key
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                copied[key] = {**self.ws.artifact_ref(src),
                               "bundle_path": dst.relative_to(exp_dir).as_posix()}
            return copied[key]

        registry_path = ROOT / "factors" / "registry.yaml"
        registry = load_yaml(registry_path)
        factors = {f["id"]: f for f in registry.get("factors", [])}

        def freeze_profile(rel: str) -> dict:
            profile_ref = freeze(rel, "agents")
            profile = load_yaml(self.ws.path(rel))
            prompt_ref = freeze(profile["prompt"], "agents")
            output_ref = freeze(profile["output_contract"], "agents")
            input_ref = freeze(profile["input_view"], "agents")
            factor_refs = []
            for factor_key in profile.get("factors", []):
                factor_id, _, version = factor_key.partition("@")
                if factor_id not in factors:
                    raise WorkspaceError(f"未注册因子：{factor_id}")
                factor = factors[factor_id]
                if factor.get("version") != version:
                    raise WorkspaceError(
                        f"因子版本不匹配：{factor_key} != {factor.get('version')}")
                factor_refs.append({"id": factor_id, "version": version,
                                    **freeze(factor["file"], "factors")})
            user_watchlist = profile.get("user_watchlist", "disabled")
            return {
                "profile": profile_ref, "prompt": prompt_ref, "input_view": input_ref,
                "output_contract": output_ref, "factors": factor_refs,
                "user_watchlist": user_watchlist,
                "queries": profile.get("queries", []),
                "data_requirements": profile.get("data_requirements", []),
            }

        lock = {
            "lock_version": "bundle3.0",
            "experiment_id": config.get("experiment_id"),
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "experiment": self.ws.artifact_ref(experiment_path),
            "data": freeze(config["data"]["recipe"]),
            "views": {k: freeze(v) for k, v in config.get("views", {}).items()},
            "judge": freeze_profile(config["judge"]["profile"]),
            "arms": [],
            "initial_state": {},
            "broker": freeze(config["broker"]["profile"]),
            "evaluation": freeze(config["evaluation"]["profile"]),
            "curator": freeze("agents/curator/profiles/default.yaml", "agents"),
            "query_templates": freeze("configs/queries/templates.yaml"),
            "factor_registry": self.ws.artifact_ref(registry_path),
            "scripts_sha256": code_hash(),
            "runtime": config.get("runtime") or {},
            "snapshots": [],
        }
        for arm in config.get("arms", []):
            lock["arms"].append(
                {"id": arm["id"], "trader": freeze_profile(arm["trader"]["profile"])})

        for key, rel in (config.get("initial_state") or {}).items():
            if not rel:
                lock["initial_state"][key] = None
                continue
            src = self.ws.path(rel)
            dst = exp_dir / "state" / "initial" / f"{key}.yaml"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            lock["initial_state"][key] = {
                **self.ws.artifact_ref(src),
                "frozen_path": dst.relative_to(exp_dir).as_posix(),
                "frozen_sha256": sha256_file(dst),
            }

        for day in config.get("snapshot_dates", []):
            snapshot = ROOT / "data" / "snapshots" / str(day) / "snapshot.yaml"
            if not snapshot.exists():
                raise WorkspaceError(f"snapshot 缺失：{snapshot}")
            lock["snapshots"].append(self.ws.artifact_ref(snapshot))

        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(yaml.safe_dump(lock, allow_unicode=True, sort_keys=False),
                             encoding="utf-8")
        print(f"[bundle] {lock_path}")
        print(f"[assets] {len(copied)} frozen text files; "
              f"snapshots={len(lock['snapshots'])}; arms={len(lock['arms'])}")
        return lock_path

    # ------------------------------------------------------------------ #
    # watchlist 更新应用（原 apply_watchlist_updates.py）
    # ------------------------------------------------------------------ #
    def apply_watchlist_updates(self, review_path: str, target: str,
                                snapshot: str | None = None) -> Path:
        """应用 Judge 输出的 watchlist_updates 到 judge watchlist 并冻结当日快照。

        Args:
            review_path: review.json 路径。
            target: judge watchlist YAML（不允许 user watchlist）。
            snapshot: 历史快照路径；默认 target 同级 history/<as_of>.yaml。

        Returns:
            快照路径。
        """
        review_file = self.ws.path(review_path)
        target_file = self.ws.path(target)
        if "user" in target_file.parts:
            raise WorkspaceError("user watchlist 只能由用户修改")
        review = json.loads(review_file.read_text(encoding="utf-8"))
        schema = json.loads(REVIEW_SCHEMA.read_text(encoding="utf-8"))
        errors = list(Draft7Validator(schema).iter_errors(review))
        if errors:
            raise WorkspaceError(f"review 契约校验失败：{errors[0].message}")
        state = load_yaml(target_file) or {"stocks": []}
        stocks = state.setdefault("stocks", [])

        for update in review.get("watchlist_updates", []):
            code = update["ts_code"]
            matches = [s for s in stocks if s.get("ts_code") == code]
            operation = update["operation"]
            if operation == "add" and not matches:
                stocks.append({
                    "ts_code": code, "theme_id": update.get("theme_id"),
                    "role": update.get("role"), "level": update.get("level", "npc"),
                    "status": "关注", "reason": update["reason"],
                    "label_source": "judge", "added_date": review["as_of"],
                    "available_at": review["as_of"],
                })
            elif matches:
                item = matches[0]
                if operation == "remove":
                    item["status"] = "移出"
                elif operation in ("upgrade", "downgrade"):
                    item["level"] = update.get("level", item.get("level"))
                if update.get("role"):
                    item["role"] = update["role"]
                item["reason"] = update["reason"]
                item["available_at"] = review["as_of"]

        state["updated"] = review["as_of"]
        state["label_source"] = "judge"
        rendered = yaml.safe_dump(state, allow_unicode=True, sort_keys=False)
        snap_path = (self.ws.path(snapshot) if snapshot
                     else target_file.parent / "history" / f"{review['as_of']}.yaml")
        if snap_path.exists() and snap_path.read_text(encoding="utf-8") != rendered:
            raise WorkspaceError(f"历史快照已存在且内容不同，拒绝覆盖：{snap_path}")
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text(rendered, encoding="utf-8")
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        if not snap_path.exists():
            snap_path.write_text(rendered, encoding="utf-8")
        print(f"[watchlist] updates={len(review.get('watchlist_updates', []))} "
              f"target={target_file}")
        print(f"[snapshot] {snap_path}")
        return snap_path
