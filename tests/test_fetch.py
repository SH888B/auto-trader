"""取数调度（Fetcher）单元测试：状态机、重试、空响应、原子落盘、对账。"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from autotrader.core import Workspace
from autotrader.fetch import Fetcher
from autotrader.ledger import FetchLedger, RawStore
from autotrader.relay import RelayError
from autotrader.sources import TZ, Sources

# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #

class Clock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **kwargs: Any) -> None:
        self.moment += timedelta(**kwargs)


class FakeClient:
    """可编排的 TushareClient 替身：按 api 队列返回响应或抛异常。"""

    def __init__(self) -> None:
        self.responses: dict[str, list[Any]] = {}
        self.pages: dict[str, list[Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.call_log: list[dict[str, Any]] = []

    def queue(self, api: str, *items: Any) -> None:
        self.responses.setdefault(api, []).extend(items)

    def queue_pages(self, api: str, *items: Any) -> None:
        self.pages.setdefault(api, []).extend(items)

    def _next(self, bucket: dict[str, list[Any]], api: str) -> Any:
        item = bucket[api].pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def call(self, api: str, params: dict[str, Any], timeout: int | None = None) -> Any:
        self.calls.append((api, dict(params)))
        self.call_log.append({"api": api, "endpoint": "primary"})
        payload = self._next(self.responses, api)
        # 模拟官方协议：{"code": 0, "data": {fields, items}}
        if isinstance(payload, dict) and "data" not in payload:
            return {"code": 0, "data": payload}
        return payload

    def call_paginated(self, api: str, params: dict[str, Any], *,
                       page_size: int = 5000, max_pages: int = 10) -> Any:
        self.calls.append((api, dict(params)))
        self.call_log.append({"api": api, "endpoint": "primary"})
        return self._next(self.pages, api)


def body(rows: int = 5, fields: tuple[str, ...] = ("ts_code", "trade_date")) -> dict:
    return {"fields": list(fields),
            "items": [[f"S{i}", "20261028"] for i in range(rows)]}


def build_sources() -> Sources:
    return Sources({
        "version": "test",
        "defaults": {"retry": {"attempts": 3, "backoff": [0], "jitter": 0},
                     "confirm_gap_minutes": 10},
        "apis": {
            "daily": {
                "api": "daily", "window": "evening", "key": "{date}",
                "params": {"trade_date": "{date}"},
                "page": {"size": 5000, "pk": ["ts_code"]},
                "expect": {"min_rows": 3, "fields": ["ts_code"]},
                "avail": {"after": "15:30"}, "class": "block",
            },
            "kpl_list": {
                "api": "kpl_list", "window": "morning", "key": "{date}_涨停",
                "params": {"trade_date": "{date}", "tag": "涨停"},
                "expect": {"min_rows": 1, "allow_empty": True, "empty_is_gap": True},
                "avail": {"day_offset": 1, "after": "08:30"}, "class": "enrich",
            },
            "limit_list_d_down": {
                "api": "limit_list_d", "window": "evening", "key": "{date}_down",
                "params": {"trade_date": "{date}", "limit_type": "D"},
                "expect": {"min_rows": 1, "allow_empty": True},
                "avail": {"after": "17:00"}, "class": "enrich",
            },
            "reports": {
                "api": "reports", "window": "evening", "key": "{date}",
                "params": {"trade_date": "{date}"},
                "expect": {"min_rows": 1},
                "avail": {"after": "20:00"}, "class": "optional",
            },
        },
    })


def make_fetcher(tmp_path: Path, client: FakeClient, clock: Clock) -> Fetcher:
    return Fetcher(
        workspace=Workspace(tmp_path),
        sources=build_sources(),
        ledger=FetchLedger(tmp_path / "data" / "meta" / "fetch_ledger.jsonl"),
        raw=RawStore(tmp_path / "data" / "raw"),
        client_factory=lambda: client,
        clock=clock,
        sleep=lambda seconds: clock.advance(seconds=seconds),
    )


def moment(hour: int, minute: int = 0, day: int = 28) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


# --------------------------------------------------------------------------- #
# 状态机
# --------------------------------------------------------------------------- #

def test_not_due_does_not_call_api(tmp_path: Path) -> None:
    client, clock = FakeClient(), Clock(moment(15, 0))
    fetcher = make_fetcher(tmp_path, client, clock)

    result = fetcher.sync("20261028", window="evening", only=["daily"])

    assert result["results"]["daily"]["status"] == "not_due"
    assert client.calls == []
    assert result["readiness"]["status"] == "ready"


def test_ok_writes_raw_and_ledger(tmp_path: Path) -> None:
    client, clock = FakeClient(), Clock(moment(16, 0))
    client.queue_pages("daily", (["ts_code"], body(5)["items"]))
    fetcher = make_fetcher(tmp_path, client, clock)

    record = fetcher.fetch_one("daily", "2026-10-28")

    assert record["status"] == "ok"
    assert record["rows"] == 5
    assert (tmp_path / "data" / "raw" / "daily" / "daily_20261028.json").is_file()
    assert fetcher.ledger.latest("2026-10-28")["daily"]["status"] == "ok"
    assert fetcher.verify("2026-10-28") == []


def test_schema_bad_does_not_write_raw(tmp_path: Path) -> None:
    client, clock = FakeClient(), Clock(moment(16, 0))
    client.queue_pages("daily", (["ts_code"], body(2)["items"]))  # min_rows=3
    fetcher = make_fetcher(tmp_path, client, clock)

    record = fetcher.fetch_one("daily", "2026-10-28")

    assert record["status"] == "schema_bad"
    assert not (tmp_path / "data" / "raw" / "daily" / "daily_20261028.json").exists()
    assert fetcher.status("2026-10-28")["blocking"] == ["daily"]


def test_empty_pending_then_confirmed(tmp_path: Path) -> None:
    client, clock = FakeClient(), Clock(moment(8, 31, day=29))
    fetcher = make_fetcher(tmp_path, client, clock)
    fetcher.raw.put("daily", "20261028", body(5))  # 让 daily 保持 ok，聚焦 kpl

    client.queue("kpl_list", {"fields": [], "items": []})
    first = fetcher.fetch_one("kpl_list", "2026-10-28")
    assert first["status"] == "empty_pending"

    clock.advance(minutes=15)
    client.queue("kpl_list", {"fields": [], "items": []})
    second = fetcher.fetch_one("kpl_list", "2026-10-28")
    assert second["status"] == "empty_confirmed"

    readiness = fetcher.status("2026-10-28")
    assert readiness["status"] == "degraded"
    assert "kpl_list" in readiness["degraded_by"]


def test_declared_empty_without_gap_is_ok(tmp_path: Path) -> None:
    """跌停池为空是合法结果（empty_is_gap=False），确认后不降级。"""
    client, clock = FakeClient(), Clock(moment(18, 30))
    fetcher = make_fetcher(tmp_path, client, clock)
    for _ in range(2):
        client.queue("limit_list_d", {"fields": ["ts_code"], "items": []})
        fetcher.fetch_one("limit_list_d_down", "2026-10-28")

    readiness = fetcher.status("2026-10-28")
    assert readiness["detail"]["limit_list_d_down"]["state"] == "empty_confirmed"
    assert "limit_list_d_down" not in readiness["degraded_by"]


# --------------------------------------------------------------------------- #
# 重试与失败分类
# --------------------------------------------------------------------------- #

def test_timeout_retried_until_ok(tmp_path: Path) -> None:
    client, clock = FakeClient(), Clock(moment(16, 0))
    client.queue_pages("daily", TimeoutError("read timed out"),
                       TimeoutError("read timed out"),
                       (["ts_code"], body(5)["items"]))
    fetcher = make_fetcher(tmp_path, client, clock)

    result = fetcher.sync("20261028", window="evening", max_attempts=3, only=["daily"])

    assert result["results"]["daily"]["status"] == "ok"
    assert len(client.calls) == 3
    assert fetcher.ledger.attempts("daily", "2026-10-28", 1) == 3


def test_40101_is_final_and_not_retried(tmp_path: Path) -> None:
    client, clock = FakeClient(), Clock(moment(16, 0))
    client.queue_pages("daily", RelayError("primary 不支持该接口(40101)"))
    fetcher = make_fetcher(tmp_path, client, clock)

    result = fetcher.sync("20261028", window="evening", max_attempts=3, only=["daily"])

    assert result["results"]["daily"]["status"] == "failed_final"
    assert len(client.calls) == 1


def test_sync_skips_settled_source(tmp_path: Path) -> None:
    client, clock = FakeClient(), Clock(moment(16, 0))
    client.queue_pages("daily", (["ts_code"], body(5)["items"]))
    fetcher = make_fetcher(tmp_path, client, clock)
    fetcher.sync("20261028", window="evening", only=["daily"])

    client.calls.clear()
    fetcher.sync("20261028", window="evening", only=["daily"])
    assert client.calls == []


def test_force_bypasses_not_due(tmp_path: Path) -> None:
    client, clock = FakeClient(), Clock(moment(15, 0))
    client.queue_pages("daily", (["ts_code"], body(5)["items"]))
    fetcher = make_fetcher(tmp_path, client, clock)

    record = fetcher.fetch_one("daily", "2026-10-28", force=True)
    assert record["status"] == "ok"


# --------------------------------------------------------------------------- #
# 旧数据回退与对账
# --------------------------------------------------------------------------- #

def test_legacy_raw_counts_as_ok(tmp_path: Path) -> None:
    client, clock = FakeClient(), Clock(moment(19, 0))
    fetcher = make_fetcher(tmp_path, client, clock)
    fetcher.raw.put("daily", "20261028", body(5))

    readiness = fetcher.status("2026-10-28")
    assert readiness["detail"]["daily"]["state"] == "ok"
    assert readiness["detail"]["daily"].get("legacy") is True


def test_verify_detects_tamper(tmp_path: Path) -> None:
    client, clock = FakeClient(), Clock(moment(16, 0))
    client.queue_pages("daily", (["ts_code"], body(5)["items"]))
    fetcher = make_fetcher(tmp_path, client, clock)
    fetcher.fetch_one("daily", "2026-10-28")

    path = tmp_path / "data" / "raw" / "daily" / "daily_20261028.json"
    path.write_text('{"fields":[],"items":[]}', encoding="utf-8")

    issues = fetcher.verify("2026-10-28")
    assert any("sha256" in issue for issue in issues)


def test_verify_detects_missing_raw(tmp_path: Path) -> None:
    client, clock = FakeClient(), Clock(moment(16, 0))
    client.queue_pages("daily", (["ts_code"], body(5)["items"]))
    fetcher = make_fetcher(tmp_path, client, clock)
    fetcher.fetch_one("daily", "2026-10-28")
    (tmp_path / "data" / "raw" / "daily" / "daily_20261028.json").unlink()

    issues = fetcher.verify("2026-10-28")
    assert any("raw 缺失" in issue for issue in issues)


def test_readiness_file_written(tmp_path: Path) -> None:
    client, clock = FakeClient(), Clock(moment(16, 0))
    client.queue_pages("daily", (["ts_code"], body(5)["items"]))
    fetcher = make_fetcher(tmp_path, client, clock)
    fetcher.fetch_one("daily", "2026-10-28")
    fetcher.status("2026-10-28")

    target = tmp_path / "data" / "meta" / "readiness" / "2026-10-28.yaml"
    assert target.is_file()
    assert "status:" in target.read_text(encoding="utf-8")


@pytest.mark.parametrize("rows,expected", [(5, "ok"), (0, "empty_pending")])
def test_fetch_one_status_matrix(tmp_path: Path, rows: int, expected: str) -> None:
    client, clock = FakeClient(), Clock(moment(16, 0, day=29))
    client.queue("kpl_list", {"fields": ["ts_code"], "items": body(rows)["items"]})
    fetcher = make_fetcher(tmp_path, client, clock)

    record = fetcher.fetch_one("kpl_list", "2026-10-28")
    assert record["status"] == expected
