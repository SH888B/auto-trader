# -*- coding: utf-8 -*-
"""validate 收拢测试：review / order_intents 的通过与拒绝路径。"""
from __future__ import annotations

from pathlib import Path

import pytest

from autotrader.core import Workspace, WorkspaceError, sha256_file
from autotrader.pipeline.validate import (
    ValidationResult,
    require_ok,
    validate_order_intents,
    validate_review,
)

WS = Workspace()


def _base_review(version: str | None = "v1.1") -> dict:
    review = {
        "as_of": "2026-10-28",
        "market_regime": {
            "trend_segment": "上升增量", "position": "左脚",
            "tolerance_level": "中", "tolerance_evidence": ["证据"],
            "evidence": ["证据"], "confidence": 0.6,
            "switch_day": {"is_switch_day": False, "candidates": ["T1"]},
        },
        "risks": [],
        "themes": [{"theme_id": "T1", "name": "测试题材", "rhythm_stage": "延续",
                    "invalid_if": "证伪条件", "evidence": ["证据"],
                    "confidence": 0.6}],
        "carriers": [],
        "data_gaps": [],
        "watchlist_updates": [],
    }
    if version:
        review["review_version"] = version
    return review


class TestValidateReview:
    def test_minimal_v11_passes(self) -> None:
        result = validate_review(_base_review())
        assert result.ok
        assert result.errors == []

    def test_v11_missing_rhythm_stage(self) -> None:
        review = _base_review()
        del review["themes"][0]["rhythm_stage"]
        result = validate_review(review)
        assert not result.ok
        assert any("rhythm_stage" in e for e in result.errors)

    def test_first_divergence_requires_basis(self) -> None:
        review = _base_review()
        review["themes"][0]["rhythm_stage"] = "首分"
        review["themes"][0]["stage_basis"] = "太短"
        assert not validate_review(review).ok
        review["themes"][0]["stage_basis"] = "三日组合均为放量阳线，符合首分定义"
        assert validate_review(review).ok

    def test_dangling_theme_ref_v11_error_v1_warning(self) -> None:
        review = _base_review()
        review["carriers"] = [{"ts_code": "000001.SZ", "carrier_class": "中军",
                               "theme_id": "不存在", "evidence": ["证据"],
                               "confidence": 0.6}]
        assert not validate_review(review).ok  # v1.1：错误
        review = _base_review(None)  # 无 review_version 字段 = v1 历史产物
        review["carriers"] = [{"ts_code": "000001.SZ", "carrier_class": "中军",
                               "theme_id": "不存在", "evidence": ["证据"],
                               "confidence": 0.6}]
        result = validate_review(review)
        assert result.ok  # v1：仅警告
        assert any("不存在" in w for w in result.warnings)

    def test_v12_board_basis_too_short(self) -> None:
        review = _base_review("v1.2")
        review["next_day_first_divergence_boards"] = [
            {"board_code": "885531.TI", "theme_id": "T1", "basis": "短"}]
        assert not validate_review(review).ok

    def test_v12_board_ok(self) -> None:
        review = _base_review("v1.2")
        review["next_day_first_divergence_boards"] = [
            {"board_code": "885531.TI", "board_name": "测试板块", "theme_id": "T1",
             "basis": "板块处于首分 stage，量能温和放大，龙头未现分歧迹象"}]
        assert validate_review(review).ok

    def test_schema_required_fields(self) -> None:
        result = validate_review({})
        assert not result.ok
        assert any("as_of" in e for e in result.errors)


class TestValidateIntents:
    def _write_ref_files(self, tmp_dir: Path) -> dict:
        """造 ref 目标文件并返回三件套 ref。"""
        refs = {}
        for key, name in [("judge_review_ref", "review.json"),
                          ("account_state_ref", "account.yaml"),
                          ("query_manifest_ref", "manifest.yaml")]:
            p = tmp_dir / name
            p.write_text("{}", encoding="utf-8")
            rel = f"temp/{tmp_dir.name}/{name}"
            refs[key] = {"path": rel, "sha256": sha256_file(WS.path(rel))}
        return refs

    def _base_intents(self, tmp_dir: Path, **over) -> dict:
        data = {
            "as_of": "2026-10-28", "valid_for": "2026-10-29",
            "trader_profile": {"id": "trader-first-divergence", "version": "t1.0"},
            "actions": [], "no_trade_reason": "空仓观望",
            **self._write_ref_files(tmp_dir),
        }
        data.update(over)
        return data

    def test_empty_passes(self, tmp_path: Path) -> None:
        # 注：tmp 文件名随机；ref 文件必须落在 workspace 内，故自造目录
        d = WS.path("temp/_test_intents")
        d.mkdir(parents=True, exist_ok=True)
        try:
            result = validate_order_intents(self._base_intents(d))
            assert result.ok, result.errors
        finally:
            for p in d.iterdir():
                p.unlink()
            d.rmdir()

    def test_no_trade_reason_required(self, tmp_path: Path) -> None:
        d = WS.path("temp/_test_intents")
        d.mkdir(parents=True, exist_ok=True)
        try:
            data = self._base_intents(d)
            del data["no_trade_reason"]
            assert not validate_order_intents(data).ok
        finally:
            for p in d.iterdir():
                p.unlink()
            d.rmdir()

    def test_ref_hash_mismatch(self, tmp_path: Path) -> None:
        d = WS.path("temp/_test_intents")
        d.mkdir(parents=True, exist_ok=True)
        try:
            data = self._base_intents(d)
            data["judge_review_ref"]["sha256"] = "0" * 64
            result = validate_order_intents(data)
            assert any("judge_review_ref" in e for e in result.errors)
        finally:
            for p in d.iterdir():
                p.unlink()
            d.rmdir()

    def test_buy_price_relation(self, tmp_path: Path) -> None:
        d = WS.path("temp/_test_intents")
        d.mkdir(parents=True, exist_ok=True)
        try:
            buy = {"action": "buy", "ts_code": "000001.SZ", "order_type": "limit",
                   "limit_price": 10, "stop_loss": 12, "take_profit": 11,
                   "position_pct": 0.5, "time_in_force": "day",
                   "reason": "测试买入理由依据充分", "strategy_factor_refs": []}
            data = self._base_intents(d, actions=[buy])
            assert any("stop_loss < limit_price" in e
                       for e in validate_order_intents(data).errors)
            buy.update(stop_loss=9, take_profit=11)
            data = self._base_intents(d, actions=[buy])
            assert validate_order_intents(data).ok
        finally:
            for p in d.iterdir():
                p.unlink()
            d.rmdir()

    def test_buy_total_capped(self, tmp_path: Path) -> None:
        d = WS.path("temp/_test_intents")
        d.mkdir(parents=True, exist_ok=True)
        try:
            mk = lambda pct: {"action": "buy", "ts_code": "000001.SZ",  # noqa: E731
                              "order_type": "limit", "limit_price": 10,
                              "stop_loss": 9, "take_profit": 11,
                              "position_pct": pct, "time_in_force": "day",
                              "reason": "测试买入理由依据充分", "strategy_factor_refs": []}
            data = self._base_intents(d, actions=[mk(0.6), mk(0.6)])
            assert any("合计" in e for e in validate_order_intents(data).errors)
        finally:
            for p in d.iterdir():
                p.unlink()
            d.rmdir()


class TestRequireOk:
    def test_raises_with_errors(self) -> None:
        with pytest.raises(WorkspaceError, match="校验失败"):
            require_ok(ValidationResult(errors=["e1", "e2"]), "测试")

    def test_passes_when_clean(self) -> None:
        require_ok(ValidationResult(warnings=["w"]), "测试")  # 不抛
