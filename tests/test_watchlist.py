"""Watchlist 每日全量表测试：全量写入、差异、只读时点、不可变、变更日志。"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from autotrader.core import Workspace, WorkspaceError
from autotrader.watchlist import Watchlist


@pytest.fixture()
def wl(tmp_path: Path) -> Watchlist:
    return Watchlist(Workspace(tmp_path), db_path=tmp_path / "warehouse.duckdb")


def stocks(*codes: str, **extra: str) -> list[dict]:
    return [{"ts_code": code, **extra} for code in codes]


def change_rows(wl: Watchlist) -> list[dict]:
    con = duckdb.connect(str(wl.db_path), read_only=True)
    try:
        return con.execute(
            "SELECT trade_date, ts_code, action FROM watchlist_change "
            "ORDER BY trade_date, ts_code").fetchdf().to_dict("records")
    finally:
        con.close()


def test_set_and_show_normalizes_date(wl: Watchlist) -> None:
    assert wl.set_daily("E001", "judge", "2026-10-01", stocks("a", "b")) == 2
    df = wl.show("E001", "judge", "20261001")
    assert list(df["ts_code"]) == ["a", "b"]
    assert df["trade_date"].iloc[0] == "20261001"
    assert df["first_added"].iloc[0] == "20261001"


def test_diff_add_and_remove(wl: Watchlist) -> None:
    wl.set_daily("E001", "judge", "20261001", stocks("a", "b", "c"))
    wl.set_daily("E001", "judge", "20261002", stocks("a", "b", "d"))
    diff = wl.diff("E001", "judge", "20261001", "20261002").set_index("ts_code")
    assert diff.loc["c", "change"] == "removed"
    assert diff.loc["d", "change"] == "added"
    assert "a" not in diff.index


def test_first_added_is_carried(wl: Watchlist) -> None:
    wl.set_daily("E001", "judge", "20261001", stocks("a", "b"))
    wl.set_daily("E001", "judge", "20261002", stocks("a", "b", "c"))
    df = wl.show("E001", "judge", "20261002").set_index("ts_code")
    assert df.loc["a", "first_added"] == "20261001"
    assert df.loc["c", "first_added"] == "20261002"


def test_as_of_takes_latest_not_after(wl: Watchlist) -> None:
    wl.set_daily("E001", "judge", "20261001", stocks("a"))
    wl.set_daily("E001", "judge", "20261003", stocks("a", "b"))
    assert list(wl.as_of("E001", "judge", "20261002")["ts_code"]) == ["a"]
    assert list(wl.as_of("E001", "judge", "20261003")["ts_code"]) == ["a", "b"]
    assert wl.as_of("E001", "judge", "20260930").empty


def test_same_day_is_immutable(wl: Watchlist) -> None:
    wl.set_daily("E001", "judge", "20261001", stocks("a"))
    assert wl.set_daily("E001", "judge", "20261001", stocks("a")) == 0
    with pytest.raises(WorkspaceError, match="已冻结"):
        wl.set_daily("E001", "judge", "20261001", stocks("a", "b"))
    assert wl.set_daily("E001", "judge", "20261001", stocks("a", "b"), force=True) == 2


def test_mass_removal_guard(wl: Watchlist) -> None:
    wl.set_daily("E001", "judge", "20261001", stocks(*[f"s{i}" for i in range(12)]))
    with pytest.raises(WorkspaceError, match="疑似写空"):
        wl.set_daily("E001", "judge", "20261002", stocks("s0"))
    assert wl.set_daily("E001", "judge", "20261002", stocks("s0"), force=True) == 1


def test_apply_updates_writes_full_and_change_log(wl: Watchlist) -> None:
    wl.set_daily("E001", "judge", "20261001",
                 [{"ts_code": "a", "role": "中军"}, {"ts_code": "b"}])
    stats = wl.apply("E001", "judge", "20261002", [
        {"ts_code": "a", "operation": "upgrade", "role": "龙头"},
        {"ts_code": "c", "operation": "add", "reason": "新进"},
        {"ts_code": "b", "operation": "remove", "reason": "断板"},
    ])
    assert stats == {"add": 1, "remove": 1, "update": 1, "rows": 2}
    df = wl.show("E001", "judge", "20261002").set_index("ts_code")
    assert set(df.index) == {"a", "c"}
    assert df.loc["a", "role"] == "龙头"
    assert df.loc["c", "first_added"] == "20261002"

    changes = {(r["ts_code"], r["action"]) for r in change_rows(wl)
               if r["trade_date"] == "20261002"}
    assert changes == {("a", "update"), ("b", "remove"), ("c", "add")}


def test_experiments_and_owners_are_isolated(wl: Watchlist) -> None:
    wl.set_daily("E001", "judge", "20261001", stocks("a"))
    wl.set_daily("E002", "judge", "20261001", stocks("b"))
    wl.set_daily("E001", "user", "20261001", stocks("c"))
    assert list(wl.show("E001", "judge", "20261001")["ts_code"]) == ["a"]
    assert list(wl.show("E002", "judge", "20261001")["ts_code"]) == ["b"]
    assert list(wl.show("E001", "user", "20261001")["ts_code"]) == ["c"]


def test_rejects_bad_input(wl: Watchlist) -> None:
    with pytest.raises(WorkspaceError, match="owner"):
        wl.set_daily("E001", "admin", "20261001", stocks("a"))
    with pytest.raises(WorkspaceError, match="trade_date"):
        wl.set_daily("E001", "judge", "2026-13-01", stocks("a"))
    with pytest.raises(WorkspaceError, match="重复"):
        wl.set_daily("E001", "judge", "20261001", stocks("a", "a"))
    with pytest.raises(WorkspaceError, match="行数"):
        wl.set_daily("E001", "judge", "20261001", [])
