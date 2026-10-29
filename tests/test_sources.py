"""数据源契约（sources.yaml）单元测试：可用时点、窗口、校验、就绪度折叠。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from autotrader.sources import (
    BLOCKED,
    DEGRADED,
    READY,
    TZ,
    Sources,
    compute_readiness,
    parse_date,
)


def build_sources() -> Sources:
    return Sources({
        "version": "test",
        "defaults": {"retry": {"attempts": 2}, "confirm_gap_minutes": 10},
        "apis": {
            "daily": {
                "api": "daily", "window": "evening", "key": "{date}",
                "params": {"trade_date": "{date}"},
                "page": {"size": 5000, "pk": ["ts_code"]},
                "expect": {"min_rows": 3, "fields": ["ts_code"]},
                "avail": {"day_offset": 0, "after": "15:30"},
                "class": "block",
            },
            "kpl_list": {
                "api": "kpl_list", "window": "morning", "key": "{date}_涨停",
                "params": {"trade_date": "{date}", "tag": "涨停"},
                "expect": {"min_rows": 1, "allow_empty": True, "empty_is_gap": True},
                "avail": {"day_offset": 1, "after": "08:30"},
                "class": "enrich",
            },
            "reports": {
                "api": "reports", "window": "evening", "key": "{date}",
                "params": {"trade_date": "{date}"},
                "expect": {"min_rows": 1},
                "avail": {"day_offset": 0, "after": "20:00"},
                "class": "optional",
            },
        },
    })


def at(hour: int, minute: int = 0, day: int = 28) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


# --------------------------------------------------------------------------- #
# 可用时点与窗口
# --------------------------------------------------------------------------- #

def test_available_at_respects_day_offset() -> None:
    sources = build_sources()
    assert sources["daily"].available_at("20261028") == at(15, 30)
    assert sources["kpl_list"].available_at("20261028") == at(8, 30, day=29)


def test_key_and_params_template() -> None:
    sources = build_sources()
    assert sources["kpl_list"].key("2026-10-28") == "20261028_涨停"
    assert sources["kpl_list"].params_for("20261028") == {
        "trade_date": "20261028", "tag": "涨停"}


def test_window_filtering() -> None:
    sources = build_sources()
    assert sources.in_window("all") == ["daily", "kpl_list", "reports"]
    assert sources.in_window("evening") == ["daily", "reports"]
    assert sources.in_window("morning") == ["kpl_list"]


def test_parse_date_accepts_both_formats() -> None:
    assert parse_date("20261028") == parse_date("2026-10-28")
    with pytest.raises(ValueError):
        parse_date("2026/10/28")


# --------------------------------------------------------------------------- #
# 响应校验
# --------------------------------------------------------------------------- #

def test_validate_rejects_short_and_missing_fields() -> None:
    sources = build_sources()
    assert sources.validate("daily", {"fields": ["ts_code"],
                                      "items": [["A"], ["B"]]}) != []
    assert sources.validate("daily", {"fields": ["close"],
                                      "items": [["A"], ["B"], ["C"]]}) != []


def test_validate_rejects_duplicate_primary_key() -> None:
    sources = build_sources()
    errors = sources.validate("daily", {"fields": ["ts_code"],
                                        "items": [["A"], ["A"], ["B"]]})
    assert any("主键" in e for e in errors)


def test_validate_allows_declared_empty() -> None:
    sources = build_sources()
    assert sources.validate("kpl_list", {"fields": [], "items": []}) == []
    # daily 不允许空
    assert sources.validate("daily", {"fields": [], "items": []}) != []


# --------------------------------------------------------------------------- #
# 就绪度折叠
# --------------------------------------------------------------------------- #

def test_not_due_is_not_a_gap() -> None:
    """19:00 时 kpl 还没到可用时间，不应把当天判成 degraded。"""
    sources = build_sources()
    readiness = compute_readiness(
        "2026-10-28", sources,
        {"daily": {"state": "ok"}, "kpl_list": {"state": "not_due"}},
        at(19, 0),
    )
    assert readiness.status == READY
    assert readiness.gaps == []


def test_block_missing_blocks() -> None:
    sources = build_sources()
    readiness = compute_readiness(
        "2026-10-28", sources,
        {"daily": {"state": "schema_bad"}, "kpl_list": {"state": "not_due"}},
        at(19, 0),
    )
    assert readiness.status == BLOCKED
    assert readiness.blocking == ["daily"]


def test_enrich_empty_confirmed_degrades() -> None:
    sources = build_sources()
    readiness = compute_readiness(
        "2026-10-28", sources,
        {"daily": {"state": "ok"}, "kpl_list": {"state": "empty_confirmed"}},
        at(9, 0, day=29),
    )
    assert readiness.status == DEGRADED
    assert readiness.degraded_by == ["kpl_list"]


def test_optional_never_affects_status() -> None:
    sources = build_sources()
    readiness = compute_readiness(
        "2026-10-28", sources,
        {"daily": {"state": "ok"}, "kpl_list": {"state": "not_due"},
         "reports": {"state": "failed"}},
        at(19, 0),
    )
    assert readiness.status == READY
    assert "reports" not in readiness.gaps


def test_readiness_is_serializable() -> None:
    sources = build_sources()
    payload = compute_readiness(
        "2026-10-28", sources, {"daily": {"state": "ok"}}, at(19, 0)).to_dict()
    assert payload["readiness_version"] == "r1.0"
    assert payload["detail"]["daily"]["class"] == "block"
    # decided_at 可解析回 datetime
    datetime.fromisoformat(payload["decided_at"])
    assert (at(19, 0) - at(18, 0)) == timedelta(hours=1)
