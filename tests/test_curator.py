# -*- coding: utf-8 -*-
"""Curator 单元测试：纯函数、渲染确定性与 digest hash 回归。"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd
import pytest

from autotrader.curator import (
    Curator,
    fmt,
    fmt_cell,
    load_dictionary,
    markdown_table,
    parse_date,
)

WORKSPACE = Path(__file__).resolve().parents[1]
SNAPSHOT = WORKSPACE / "data" / "snapshots" / "2026-10-28" / "snapshot.yaml"


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #

class TestFmt:
    def test_none_and_nan(self) -> None:
        assert fmt(None) == ""
        assert fmt(float("nan")) == ""

    def test_float_strips_trailing_zeros(self) -> None:
        assert fmt(3.5) == "3.5"
        assert fmt(10.0) == "10"
        assert fmt(0.123456789) == "0.1235"

    def test_pipe_and_newline_escaped(self) -> None:
        assert fmt("a|b\nc") == "a\\|b c"


class TestFmtCell:
    def test_price2(self) -> None:
        assert fmt_cell(12.345, {"fmt": "price2"}) == "12.35"

    def test_pct2(self) -> None:
        assert fmt_cell(0.1234, {"fmt": "pct2"}) == "0.12"

    def test_yi8(self) -> None:
        assert fmt_cell(1.5e8, {"fmt": "yi8"}) == "1.50"

    def test_yi5(self) -> None:
        assert fmt_cell(1.5e5, {"fmt": "yi5"}) == "1.50"

    def test_int0(self) -> None:
        assert fmt_cell(42, {"fmt": "int0"}) == "42"

    def test_none_with_spec(self) -> None:
        assert fmt_cell(None, {"fmt": "price2"}) == ""

    def test_string_falls_back_to_fmt(self) -> None:
        assert fmt_cell("600519.SH", {"fmt": "price2"}) == "600519.SH"


class TestMarkdownTable:
    def test_empty(self) -> None:
        df = pd.DataFrame()
        assert markdown_table(df, [], 10, {}, set()) == "（无数据）"

    def test_missing_columns(self) -> None:
        df = pd.DataFrame({"a": [1]})
        assert markdown_table(df, ["nope"], 10, {}, set()) == "（配置列均不存在）"

    def test_exclude_and_limit_and_zh_header(self) -> None:
        df = pd.DataFrame({"code": ["x", "y", "z"], "secret": [1, 2, 3],
                           "pct": [0.01, 0.02, 0.03]})
        table = markdown_table(df, ["code", "secret", "pct"], 2,
                               {"pct": {"zh": "涨跌幅"}}, {"secret"})
        lines = table.split("\n")
        assert lines[0] == "| code | 涨跌幅 |"
        assert len(lines) == 4  # header + separator + 2 rows(limit=2)


class TestParseDate:
    def test_str(self) -> None:
        assert parse_date("2026-10-28").isoformat() == "2026-10-28"

    def test_date_passthrough(self) -> None:
        from datetime import date
        assert parse_date(date(2026, 10, 28)) == date(2026, 10, 28)

    def test_bad_format(self) -> None:
        with pytest.raises(ValueError):
            parse_date("20261028")


class TestDictionary:
    def test_loads_columns(self) -> None:
        columns, exclude = load_dictionary()
        assert isinstance(columns, dict)
        assert "ts_code" in columns
        assert isinstance(exclude, set)


# --------------------------------------------------------------------------- #
# digest hash 回归：旧入口重渲染 2026-10-28 judge digest，字节级一致
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not SNAPSHOT.exists(), reason="无 2026-10-28 快照，无法回归")
def test_render_digest_regression() -> None:
    """渲染确定性回归：与冻结基线比对 digest 字节 hash。

    基线存 temp/baselines/（gitignore），首次运行自动生成并 skip；
    此后任何代码或数据变动导致渲染结果变化都会失败。
    """
    import shutil

    baseline_dir = WORKSPACE / "temp" / "baselines"
    baseline_file = baseline_dir / "curator_digest_20261028.sha256"
    out_dir = WORKSPACE / "temp" / "_regression_curator"
    out = out_dir / "digest.md"
    try:
        Curator().render_digest("20261028", output=str(out), run_id="live-20261028")
        new_hash = hashlib.sha256(out.read_bytes()).hexdigest()
        if not baseline_file.exists():
            baseline_dir.mkdir(parents=True, exist_ok=True)
            baseline_file.write_text(new_hash, encoding="ascii")
            pytest.skip("基线已生成于 temp/baselines/，下次运行将比对")
        baseline_hash = baseline_file.read_text(encoding="ascii").strip()
        assert new_hash == baseline_hash, (
            "digest 渲染与冻结基线不一致："
            f"baseline={baseline_hash[:16]} new={new_hash[:16]}"
        )
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
