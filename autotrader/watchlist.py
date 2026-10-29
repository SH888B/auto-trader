"""Watchlist：每天全量清单 + 变更日志（DuckDB 两张表）。

设计（用户 2026-10-30 拍板）：

- 一个 ``(experiment_id, owner, trade_date)`` = 一份不可变清单；
- 每天写全量，两天的差异就是"谁进了、谁出了"；
- 历史只写一次：同一天重复写且内容不同 → 拒绝，除非显式 ``force``；
- ``trade_date`` 是"哪天收盘后定的"（YYYYMMDD），Judge 在 T 日读 ``<= T-1``；
- ``watchlist_change`` 只记"为什么变"，不参与"看到什么"的查询。

Scout 不读也不写这些表；只有 Curator / CLI 使用。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import duckdb
import pandas as pd

from .core import Workspace, WorkspaceError, trade_date

DB_PATH = "data/warehouse.duckdb"
SCHEMA_FILE = Path(__file__).resolve().parents[1] / "contracts" / "schema_v5.sql"
OWNERS = ("judge", "user")

#: "看到什么"用的业务列（不含 gmt_created）
SHOW_COLUMNS = ["experiment_id", "owner", "trade_date", "ts_code", "name",
                "role", "level", "theme_id", "reason", "first_added"]


def _check_experiment(experiment: str) -> str:
    experiment = str(experiment or "").strip()
    if not experiment or any(c in experiment for c in "\\/:*?\"<>|"):
        raise WorkspaceError(f"实验号非法：{experiment!r}")
    return experiment


def _check_owner(owner: str) -> str:
    if owner not in OWNERS:
        raise WorkspaceError(f"owner 必须是 {OWNERS}：{owner!r}")
    return owner


def _normalize_stock(item: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(item, str):
        item = {"ts_code": item}
    code = str(item.get("ts_code") or "").strip()
    if not code:
        raise WorkspaceError(f"watchlist 条目缺少 ts_code：{item!r}")
    return {
        "ts_code": code,
        "name": item.get("name"),
        "role": item.get("role"),
        "level": item.get("level"),
        "theme_id": item.get("theme_id"),
        "reason": item.get("reason"),
    }


class Watchlist:
    """每天全量 watchlist 的读写。"""

    def __init__(self, workspace: Workspace | None = None,
                 db_path: str | Path | None = None) -> None:
        self.ws = workspace or Workspace()
        self.db_path = Path(db_path) if db_path else self.ws.path(DB_PATH)

    # ------------------------------------------------------------------ #
    # 基础设施
    # ------------------------------------------------------------------ #
    def _connect(self) -> duckdb.DuckDBPyConnection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(self.db_path))
        con.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
        return con

    # ------------------------------------------------------------------ #
    # 写：整日全量
    # ------------------------------------------------------------------ #
    def set_daily(self, experiment: str, owner: str, date: str | Any,
                  stocks: Iterable[str | dict[str, Any]], *,
                  force: bool = False) -> int:
        """写入某日全量清单；返回写入行数。

        Raises:
            WorkspaceError: 日期已冻结且内容不同（除非 force）、行数异常、
                或单日删除比例过高（防"写空"事故）。
        """
        experiment = _check_experiment(experiment)
        owner = _check_owner(owner)
        day = trade_date(date)
        rows = [_normalize_stock(item) for item in stocks]
        codes = [r["ts_code"] for r in rows]
        if len(codes) != len(set(codes)):
            raise WorkspaceError(f"{experiment}/{owner}/{day} 存在重复 ts_code")
        if not 1 <= len(rows) <= 200:
            raise WorkspaceError(f"{experiment}/{owner}/{day} 行数异常：{len(rows)}")

        con = self._connect()
        try:
            con.execute("BEGIN TRANSACTION")
            existing = self._rows(con, experiment, owner, day)
            if existing and self._same(existing, rows):
                con.execute("COMMIT")
                return 0
            if existing and not force:
                con.execute("ROLLBACK")
                raise WorkspaceError(
                    f"{experiment}/{owner}/{day} 已冻结且内容不同；"
                    "历史不可改，确需重写用 force")
            previous = self._previous_rows(con, experiment, owner, day)
            self._guard_shrink(previous, rows, force=force)

            prev_map = {r["ts_code"]: r for r in previous}
            old_map = {r["ts_code"]: r for r in existing}
            final = []
            for row in rows:
                code = row["ts_code"]
                first = (prev_map.get(code) or old_map.get(code) or {}).get("first_added") or day
                final.append({**row, "first_added": first})

            changes = self._changes(prev_map, {r["ts_code"]: r for r in final}, day)
            con.execute(
                "DELETE FROM watchlist_daily WHERE experiment_id=? AND owner=? AND trade_date=?",
                [experiment, owner, day])
            con.executemany(
                """INSERT INTO watchlist_daily
                   (experiment_id, owner, trade_date, ts_code, name, role, level,
                    theme_id, reason, first_added)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [[experiment, owner, day, r["ts_code"], r["name"], r["role"],
                  r["level"], r["theme_id"], r["reason"], r["first_added"]]
                 for r in final])
            if changes:
                con.executemany(
                    """INSERT INTO watchlist_change
                       (experiment_id, owner, trade_date, ts_code, action,
                        role, level, reason)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    [[experiment, owner, day, c["ts_code"], c["action"],
                      c["role"], c["level"], c["reason"]] for c in changes])
            con.execute("COMMIT")
            return len(final)
        finally:
            con.close()

    def apply(self, experiment: str, owner: str, date: str | Any,
              updates: Iterable[dict[str, Any]], *, force: bool = False) -> dict[str, int]:
        """按 Judge 的 ``watchlist_updates`` 生成当日全量。

        输入是"变化"（add/remove/upgrade/downgrade），输出是当天全量。
        基准是 ``< date`` 的最近一份清单。
        """
        experiment = _check_experiment(experiment)
        owner = _check_owner(owner)
        day = trade_date(date)
        base = self.as_of(experiment, owner, self._previous_day(day))
        current = {r["ts_code"]: dict(r) for _, r in base.iterrows()}

        counts = {"add": 0, "remove": 0, "update": 0}
        for raw in updates:
            item = _normalize_stock(raw)
            code = item["ts_code"]
            action = str(raw.get("operation") or raw.get("action") or "update")
            if action == "remove":
                if current.pop(code, None) is not None:
                    counts["remove"] += 1
                continue
            if code in current:
                merged = {**current[code],
                          **{k: v for k, v in item.items() if v is not None}}
                if self._business_changed(current[code], merged):
                    counts["update"] += 1
                current[code] = merged
            else:
                current[code] = item
                counts["add"] += 1

        written = self.set_daily(experiment, owner, day, list(current.values()),
                                 force=force)
        return {**counts, "rows": written}

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #
    def show(self, experiment: str, owner: str, date: str | Any) -> pd.DataFrame:
        """某一天的精确全量（不存在返回空表）。"""
        experiment = _check_experiment(experiment)
        owner = _check_owner(owner)
        day = trade_date(date)
        con = self._connect()
        try:
            return self._frame(con.execute(
                f"SELECT {', '.join(SHOW_COLUMNS)} FROM watchlist_daily "
                "WHERE experiment_id=? AND owner=? AND trade_date=? "
                "ORDER BY ts_code", [experiment, owner, day]).fetchdf())
        finally:
            con.close()

    def as_of(self, experiment: str, owner: str, as_of: str | Any) -> pd.DataFrame:
        """``<= as_of`` 的最近一份清单（调用方自行决定传 T 还是 T-1）。"""
        experiment = _check_experiment(experiment)
        owner = _check_owner(owner)
        cutoff = trade_date(as_of)
        con = self._connect()
        try:
            return self._frame(con.execute(
                f"""SELECT {', '.join(SHOW_COLUMNS)} FROM watchlist_daily
                    WHERE experiment_id=? AND owner=?
                      AND trade_date = (
                          SELECT max(trade_date) FROM watchlist_daily
                          WHERE experiment_id=? AND owner=? AND trade_date <= ?)
                    ORDER BY ts_code""",
                [experiment, owner, experiment, owner, cutoff]).fetchdf())
        finally:
            con.close()

    def diff(self, experiment: str, owner: str,
             left: str | Any, right: str | Any) -> pd.DataFrame:
        """两天的差异：added / removed / updated。"""
        a = self.show(experiment, owner, left)
        b = self.show(experiment, owner, right)
        merged = a.merge(b, on="ts_code", how="outer", suffixes=("_old", "_new"),
                         indicator=True)
        merged["change"] = "updated"
        merged.loc[merged["_merge"] == "left_only", "change"] = "removed"
        merged.loc[merged["_merge"] == "right_only", "change"] = "added"
        unchanged = merged.apply(
            lambda r: r["change"] == "updated" and all(
                r.get(f"{col}_old") == r.get(f"{col}_new")
                for col in ("name", "role", "level", "theme_id", "reason")), axis=1)
        merged = merged[~unchanged]
        return merged.loc[:, ["ts_code", "change", "name_old", "name_new",
                              "role_old", "role_new", "reason_old", "reason_new"]]

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    @staticmethod
    def _previous_day(day: str) -> str:
        """上一个日历日（仅用于"取 < day 的最近一份"这个上界）。"""
        from datetime import datetime, timedelta
        prev = datetime.strptime(day, "%Y%m%d") - timedelta(days=1)
        return prev.strftime("%Y%m%d")

    def _rows(self, con: duckdb.DuckDBPyConnection, experiment: str,
              owner: str, day: str) -> list[dict[str, Any]]:
        return con.execute(
            f"SELECT {', '.join(SHOW_COLUMNS)} FROM watchlist_daily "
            "WHERE experiment_id=? AND owner=? AND trade_date=? ORDER BY ts_code",
            [experiment, owner, day]).fetchdf().to_dict("records")

    def _previous_rows(self, con: duckdb.DuckDBPyConnection, experiment: str,
                       owner: str, day: str) -> list[dict[str, Any]]:
        return con.execute(
            f"""SELECT {', '.join(SHOW_COLUMNS)} FROM watchlist_daily
                WHERE experiment_id=? AND owner=?
                  AND trade_date = (
                      SELECT max(trade_date) FROM watchlist_daily
                      WHERE experiment_id=? AND owner=? AND trade_date < ?)
                ORDER BY ts_code""",
            [experiment, owner, experiment, owner, day]).fetchdf().to_dict("records")

    @staticmethod
    def _same(existing: list[dict[str, Any]], rows: list[dict[str, Any]]) -> bool:
        def key(r: dict[str, Any]) -> tuple:
            return (r["ts_code"], r.get("name"), r.get("role"), r.get("level"),
                    r.get("theme_id"), r.get("reason"))
        return sorted(key(r) for r in existing) == sorted(key(r) for r in rows)

    @staticmethod
    def _business_changed(old: dict[str, Any], new: dict[str, Any]) -> bool:
        return any(old.get(k) != new.get(k)
                   for k in ("name", "role", "level", "theme_id", "reason"))

    @staticmethod
    def _guard_shrink(previous: list[dict[str, Any]], rows: list[dict[str, Any]],
                      *, force: bool) -> None:
        if force or not previous or len(previous) < 10:
            return
        old = {r["ts_code"] for r in previous}
        new = {r["ts_code"] for r in rows}
        removed = len(old - new)
        if removed / len(old) > 0.5:
            raise WorkspaceError(
                f"单日删除 {removed}/{len(old)} 只（>50%），疑似写空；"
                "确认后加 force")

    @staticmethod
    def _changes(previous: dict[str, dict[str, Any]],
                 current: dict[str, dict[str, Any]],
                 day: str) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        for code, row in current.items():
            if code not in previous:
                changes.append({"ts_code": code, "action": "add", **row})
            elif Watchlist._business_changed(previous[code], row):
                changes.append({"ts_code": code, "action": "update", **row})
        for code, row in previous.items():
            if code not in current:
                changes.append({"ts_code": code, "action": "remove", **row})
        return changes

    @staticmethod
    def _frame(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame(columns=SHOW_COLUMNS)
        return df.loc[:, SHOW_COLUMNS]
