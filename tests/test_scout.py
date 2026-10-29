"""Scout 单元测试与快照回归测试。

单元测试覆盖纯函数与数仓幂等性（无网络）；回归测试用 10-28 冻结基线验证
finalize 重冻结后数据字段逐一致（代码版本字段允许变化）。
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pandas as pd
import pytest
import yaml

import autotrader.scout as scout
from autotrader.scout import Scout, _candle_pattern, missing_files, to_date, universe_filter

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "temp" / "regression_baseline_20261028.yaml"

# --------------------------------------------------------------------------- #
# 纯函数单元测试
# --------------------------------------------------------------------------- #

def test_candle_pattern_bullish() -> None:
    row = {"open": 10.0, "high": 11.0, "low": 9.8, "close": 10.9, "pct_chg": 4.0}
    assert _candle_pattern(row) == "大阳线"


def test_candle_pattern_bearish_and_upper_shadow() -> None:
    row = {"open": 11.0, "high": 12.0, "low": 10.9, "close": 10.1, "pct_chg": -4.0}
    assert _candle_pattern(row) == "大阴线"
    row2 = {"open": 10.0, "high": 12.0, "low": 9.9, "close": 10.1, "pct_chg": 1.0}
    assert "长上影" in _candle_pattern(row2)


def test_candle_pattern_flat_range() -> None:
    row = {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "pct_chg": 0.0}
    assert _candle_pattern(row) == "无"


def test_universe_filter_prefix_and_st() -> None:
    df = pd.DataFrame({"ts_code": ["000001.SZ", "300001.SZ", "880001.BJ", "600001.SH"]})
    universe = {"markets": {"主板创业板": {"code_prefix": ["000", "300", "600"]}},
                "exclusions": {"st_stock": True}}
    names = {"600001.SH": "ST测试"}
    out = universe_filter(df, universe, names)
    assert set(out["ts_code"]) == {"000001.SZ", "300001.SZ"}


def test_universe_filter_st_degraded_without_names() -> None:
    df = pd.DataFrame({"ts_code": ["600001.SH", "000001.SZ"]})
    universe = {"markets": {}, "exclusions": {"st_stock": True}}
    out = universe_filter(df, universe, None)  # 无名称数据降级：不过滤 ST
    assert len(out) == 2


def test_to_date_parses_yyyymmdd() -> None:
    out = to_date(pd.Series(["20261028", 20261029, "bad"]))
    assert str(out.iloc[0]) == "2026-10-28"
    assert str(out.iloc[1]) == "2026-10-29"
    assert pd.isna(out.iloc[2])


def test_missing_files_detects_absent_and_empty(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    raw = tmp_path / "raw"
    (raw / "stk_limit").mkdir(parents=True)
    (raw / "kpl_list").mkdir(parents=True)
    (raw / "limit_list_d").mkdir(parents=True)
    # stk_limit 齐、kpl 空响应 → kpl 视为缺失；limit_list_d up/down 全缺
    (raw / "stk_limit" / "stk_limit_20261028.json").write_text(
        json.dumps({"items": [[1]]}), encoding="utf-8")
    (raw / "kpl_list" / "kpl_list_20261028_涨停.json").write_text(
        json.dumps({"items": []}), encoding="utf-8")
    monkeypatch.setattr(scout, "RAW", raw)
    miss = missing_files("20261028")
    apis = {a for a, _, _ in miss}
    keys = {k for _, k, _ in miss}
    assert "20261028_涨停" in keys and "20261028_up" in keys and "20261028_down" in keys
    assert "stk_limit" not in apis  # stk_limit 已齐（limit_cpt_list/limit_step 未建目录也计入缺失）


def test_upsert_idempotent() -> None:
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE t (trade_date DATE, ts_code VARCHAR, v DOUBLE)")
    df = pd.DataFrame({"trade_date": [pd.to_datetime("2026-10-28").date()],
                       "ts_code": ["000001.SZ"], "v": [1.0]})
    assert scout.upsert(con, "t", df, ["20261028"]) == 1
    assert scout.upsert(con, "t", df, ["20261028"]) == 1  # 重跑不重复
    n = con.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    assert n == 1
    con.close()


# --------------------------------------------------------------------------- #
# 稳定性回归：10-28 快照数据字段与冻结基线一致
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not BASELINE.exists(), reason="无本地回归基线（私有环境专用）")
def test_finalize_regression_20261028() -> None:
    """重冻结 10-28 快照：数据内容字段必须与基线逐一相等。

    允许差异的字段：generated_at / backfilled / derived_code_version / scripts_sha256
    （代码版本与生成时间按定义变化）。s3.1 起 status/gaps 由 readiness 计算，
    gaps 格式改为 "source: state"，因此不再与旧基线逐字比对，改为断言结构性结果。
    derived_files 的 hash 只比对"稳定表"——经 SELECT * 或 pool 透传的文件
    （amount_top/boards/gain_top/loss_top/pit_top/limit_up/limit_up_focus/
    limit_down/watchlist_today）内含 ingested_at 审计列，任何时刻重跑字节都会变，
    这是原管道既有属性（TODO 已记录：Data Recipe 修订时剔除审计列）。

    注意：finalize 会改写真实 snapshot.yaml，而 Curator 的 digest 会引用其中的
    gaps；测试结束必须还原，否则用例执行顺序会影响 digest 回归基线。
    """
    snapshot_path = ROOT / "data" / "snapshots" / "2026-10-28" / "snapshot.yaml"
    original = snapshot_path.read_bytes()
    try:
        Scout().finalize("20261028")
        new = yaml.safe_load(snapshot_path.read_text(encoding="utf-8"))
        old = yaml.safe_load(BASELINE.read_text(encoding="utf-8"))
        for field in ("trade_date", "data_as_of", "status",
                      "data_recipe", "selectors", "universe", "selectors_version",
                      "universe_version", "derived_rows", "raw_inputs"):
            assert new[field] == old[field], (
                f"snapshot 字段漂移：{field}\n新：{new[field]}\n旧：{old[field]}")
        assert new["snapshot_version"] == "s3.1"
        assert new["status"] == "degraded"
        assert new["readiness"]["degraded_by"] == ["kpl_list"]
        assert new["readiness"]["blocking"] == []
        # 行数全表比对 + 稳定表 hash 比对
        assert set(new["derived_files"]) == set(old["derived_files"])
        stable = {"gain_week", "cm20_core", "prev_lu_feedback", "prev_ld_feedback",
                  "index_panel", "ceiling", "carrier_lifecycle"}
        for name in new["derived_files"]:
            assert new["derived_files"][name]["rows"] == old["derived_files"][name]["rows"], name
            if name in stable:
                assert (new["derived_files"][name]["sha256"]
                        == old["derived_files"][name]["sha256"]), f"{name} hash 漂移"
    finally:
        snapshot_path.write_bytes(original)
