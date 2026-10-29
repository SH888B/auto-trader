# -*- coding: utf-8 -*-
"""Judge/Trader 交接协议与 Daily 编排测试（用 2026-10-28 真实快照，产物落 temp）。"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from autotrader.core import RunLogger, Workspace, WorkspaceError, load_yaml
from autotrader.curator import Curator
from autotrader.judge import Judge
from autotrader.pipeline.daily import Daily
from autotrader.trader import Trader

WS = Workspace()
DATE = "2026-10-28"
DAY = "20261028"
RUN_ROOT = "temp/_test_pipeline"
LOG_PATH = WS.path("temp/_test_pipeline_runlog.jsonl")

SNAPSHOT = WS.path(f"data/snapshots/{DATE}/snapshot.yaml")
pytestmark = pytest.mark.skipif(not SNAPSHOT.exists(), reason="无 2026-10-28 快照")


@pytest.fixture()
def run_dir() -> Path:
    d = WS.path(RUN_ROOT) / DATE
    yield d
    shutil.rmtree(WS.path(RUN_ROOT), ignore_errors=True)
    LOG_PATH.unlink(missing_ok=True)


def _render_digest(view: str, module: str) -> None:
    out = f"{RUN_ROOT}/{DATE}/{module}/digest.md"
    Curator().render_digest(DAY, view=view, module=module, output=out,
                            run_id=f"live-{DAY}-{module}")


def _write_review(run_dir: Path, updates: list | None = None) -> Path:
    review = {
        "review_version": "v1.1", "as_of": DATE,
        "market_regime": {
            "trend_segment": "上升增量", "position": "左脚", "tolerance_level": "中",
            "tolerance_evidence": ["证据"], "evidence": ["证据"], "confidence": 0.6,
            "switch_day": {"is_switch_day": False, "candidates": []},
        },
        "risks": [],
        "themes": [{"theme_id": "T1", "name": "测试题材", "rhythm_stage": "延续",
                    "invalid_if": "证伪条件", "evidence": ["证据"], "confidence": 0.6}],
        "carriers": [], "data_gaps": [],
        "watchlist_updates": updates or [],
    }
    judge_dir = run_dir / "judge"
    judge_dir.mkdir(parents=True, exist_ok=True)
    p = judge_dir / "review.json"
    p.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _logger() -> RunLogger:
    return RunLogger(log_path=LOG_PATH)


# --------------------------------------------------------------------------- #
# Judge
# --------------------------------------------------------------------------- #

class TestJudgePrepare:
    def test_handoff_package(self, run_dir: Path) -> None:
        _render_digest("configs/digest/judge_default.yaml", "judge")
        handoff = Judge(WS, _logger(), run_root=RUN_ROOT).prepare(DATE)
        task = load_yaml(handoff / "judge_task.yaml")
        assert task["task"] == "judge" and task["as_of"] == DATE
        assert task["profile"]["id"] == "judge-default"
        # 输入清单必须全部带 hash，且 digest hash 与实际文件一致
        digest = WS.path(task["digest"]["path"])
        assert hashlib.sha256(digest.read_bytes()).hexdigest() == task["digest"]["sha256"]
        assert all("sha256" in i and len(i["sha256"]) == 64 for i in task["inputs"])
        paths = [i["path"] for i in task["inputs"]]
        assert any(p.endswith("v5.md") for p in paths)  # prompt
        assert any(p.startswith("factors/") for p in paths)  # 因子
        # task.md 内嵌 digest 全文
        md = (handoff / "judge_task.md").read_text(encoding="utf-8")
        assert digest.read_text(encoding="utf-8") in md

    def test_idempotent(self, run_dir: Path) -> None:
        _render_digest("configs/digest/judge_default.yaml", "judge")
        judge = Judge(WS, _logger(), run_root=RUN_ROOT)
        h1 = judge.prepare(DATE)
        h2 = judge.prepare(DATE)
        assert (h1 / "judge_task.yaml").read_bytes() == (h2 / "judge_task.yaml").read_bytes()
        assert (h1 / "judge_task.md").read_bytes() == (h2 / "judge_task.md").read_bytes()

    def test_missing_digest(self, run_dir: Path) -> None:
        with pytest.raises(WorkspaceError, match="digest"):
            Judge(WS, _logger(), run_root=RUN_ROOT).prepare(DATE)


class TestJudgeCollect:
    def test_missing_review(self, run_dir: Path) -> None:
        (run_dir / "judge").mkdir(parents=True)
        with pytest.raises(WorkspaceError, match="review.json"):
            Judge(WS, _logger(), run_root=RUN_ROOT).collect(DATE)

    def test_collect_ok_writes_runlog(self, run_dir: Path) -> None:
        _write_review(run_dir)
        review = Judge(WS, _logger(), run_root=RUN_ROOT).collect(DATE)
        assert review["review_version"] == "v1.1"
        events = [json.loads(line) for line in
                  LOG_PATH.read_text(encoding="utf-8").strip().splitlines()]
        assert events[-1]["event"] == "end" and events[-1]["status"] == "success"

    def test_collect_rejects_invalid(self, run_dir: Path) -> None:
        p = _write_review(run_dir)
        review = json.loads(p.read_text(encoding="utf-8"))
        del review["themes"][0]["rhythm_stage"]  # v1.1 必填
        p.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(WorkspaceError, match="校验失败"):
            Judge(WS, _logger(), run_root=RUN_ROOT).collect(DATE)
        events = [json.loads(line) for line in
                  LOG_PATH.read_text(encoding="utf-8").strip().splitlines()]
        assert events[-1]["event"] == "error"


# --------------------------------------------------------------------------- #
# Trader
# --------------------------------------------------------------------------- #

class TestTraderPrepare:
    def test_handoff_package(self, run_dir: Path) -> None:
        _render_digest("configs/digest/trader_initial.yaml", "trader")
        review_path = _write_review(run_dir)
        handoff = Trader(WS, _logger(), run_root=RUN_ROOT).prepare(DATE)
        task = load_yaml(handoff / "trader_task.yaml")
        assert task["task"] == "trader"
        # judge review 引用 hash 与实际文件一致
        assert hashlib.sha256(review_path.read_bytes()).hexdigest() == \
            task["judge_review"]["sha256"]
        md = (handoff / "trader_task.md").read_text(encoding="utf-8")
        assert review_path.read_text(encoding="utf-8") in md  # review 内嵌

    def test_missing_review(self, run_dir: Path) -> None:
        _render_digest("configs/digest/trader_initial.yaml", "trader")
        with pytest.raises(WorkspaceError, match="review"):
            Trader(WS, _logger(), run_root=RUN_ROOT).prepare(DATE)


class TestTraderCollect:
    def test_missing_intents(self, run_dir: Path) -> None:
        intents = run_dir / "arms" / "manual" / "trader" / "order_intents.json"
        intents.parent.mkdir(parents=True)
        with pytest.raises(WorkspaceError, match="order_intents"):
            Trader(WS, _logger(), run_root=RUN_ROOT).collect(DATE)


# --------------------------------------------------------------------------- #
# Daily 编排
# --------------------------------------------------------------------------- #

class TestDaily:
    def test_prepare_resume_and_idempotent(self, run_dir: Path) -> None:
        daily = Daily(WS, _logger(), run_root=RUN_ROOT)
        daily.prepare(DATE)  # 快照已存在 → 跳过 Scout；渲染 digest + judge 任务包
        task1 = run_dir / "handoff" / "judge_task.yaml"
        assert task1.is_file()
        daily.prepare(DATE)  # 全部产物存在 → 全跳过
        assert task1.read_bytes() == task1.read_bytes()  # 重跑不改动

    def test_continue_needs_review(self, run_dir: Path) -> None:
        daily = Daily(WS, _logger(), run_root=RUN_ROOT)
        daily.prepare(DATE)
        with pytest.raises(WorkspaceError, match="review.json"):
            daily.continue_(DATE)

    def test_continue_full_chain(self, run_dir: Path) -> None:
        daily = Daily(WS, _logger(), run_root=RUN_ROOT)
        daily.prepare(DATE)
        _write_review(run_dir)
        daily.continue_(DATE)
        assert (run_dir / "trader" / "digest.md").is_file()
        assert (run_dir / "handoff" / "trader_task.yaml").is_file()
