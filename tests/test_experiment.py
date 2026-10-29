# -*- coding: utf-8 -*-
"""实验模式测试：独立 state 隔离、幂等推进、不污染 live。"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from autotrader.core import RunLogger, Workspace, WorkspaceError, load_yaml
from autotrader.experiment import Experiment
from autotrader.judge import Judge

WS = Workspace()
DATE = "2026-10-28"
DAY = "20261028"
EXP_ROOT = "temp/_test_experiments"
EXP_ID = "E990_test"
LOG_PATH = WS.path("temp/_test_exp_runlog.jsonl")

SNAPSHOT = WS.path(f"data/snapshots/{DATE}/snapshot.yaml")
pytestmark = pytest.mark.skipif(not SNAPSHOT.exists(), reason="无 2026-10-28 快照")


@pytest.fixture()
def exp() -> Experiment:
    e = Experiment(EXP_ID, WS, RunLogger(log_path=LOG_PATH), exp_root=EXP_ROOT)
    yield e
    shutil.rmtree(WS.path(EXP_ROOT), ignore_errors=True)
    LOG_PATH.unlink(missing_ok=True)


def _write_review(run_root: str) -> Path:
    review = {
        "review_version": "v1.1", "as_of": DATE,
        "market_regime": {
            "trend_segment": "上升增量", "position": "左脚",
            "tolerance_level": "中", "tolerance_evidence": ["证据"],
            "evidence": ["证据"], "confidence": 0.6,
            "switch_day": {"is_switch_day": False, "candidates": []},
        },
        "risks": [],
        "themes": [{"theme_id": "T1", "name": "测试题材", "rhythm_stage": "延续",
                    "invalid_if": "证伪条件", "evidence": ["证据"],
                    "confidence": 0.6}],
        "carriers": [], "data_gaps": [],
        "watchlist_updates": [{
            "operation": "add", "ts_code": "000001.SZ", "reason": "实验测试新增",
            "level": "npc",
        }],
    }
    judge_dir = WS.path(f"{run_root}/{DATE}/judge")
    judge_dir.mkdir(parents=True, exist_ok=True)
    p = judge_dir / "review.json"
    p.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


class TestSetup:
    def test_creates_isolated_state(self, exp: Experiment) -> None:
        exp.setup()
        root = WS.path(f"{EXP_ROOT}/{EXP_ID}")
        assert (root / "runs").is_dir()
        # 基线 state 已复制且与 live 一致
        watch = WS.path(exp.watchlist_target)
        assert watch.is_file()
        assert watch.read_bytes() == WS.path(
            "state/watchlist/judge/current.yaml").read_bytes()
        # user watchlist 也是实验私有的
        assert WS.path(exp.user_watchlist_target).is_file()
        # 历史目录存在；T-1 快照在 prepare 时按需补齐（不继承当日）
        assert (root / "state/watchlist/judge/history").is_dir()

    def test_refuses_overwrite(self, exp: Experiment) -> None:
        exp.setup()
        with pytest.raises(WorkspaceError, match="拒绝覆盖"):
            exp.setup()


class TestIsolation:
    def test_collect_writes_experiment_state_only(self, exp: Experiment) -> None:
        exp.setup()
        exp.prepare(DATE)
        live_watch_before = WS.path("state/watchlist/judge/current.yaml").read_bytes()
        _write_review(exp.run_root)
        Judge(WS, RunLogger(log_path=LOG_PATH), run_root=exp.run_root,
              watchlist_target=exp.watchlist_target).collect(DATE)
        # 实验 state 已应用更新
        exp_watch = load_yaml(WS.path(exp.watchlist_target))
        assert any(s.get("ts_code") == "000001.SZ"
                   for s in exp_watch.get("stocks", []))
        # live state 未被触碰
        assert WS.path("state/watchlist/judge/current.yaml").read_bytes() == \
            live_watch_before

    def test_prepare_uses_experiment_watchlist(self, exp: Experiment) -> None:
        exp.setup()
        exp.prepare(DATE)
        task = load_yaml(WS.path(
            f"{EXP_ROOT}/{EXP_ID}/runs/{DATE}/handoff/judge_task.yaml"))
        paths = [i["path"] for i in task["inputs"]]
        assert any(p.startswith(f"{EXP_ROOT}/{EXP_ID}/state/") for p in paths)
        # Judge 输入是 T-1 的冻结快照，不是 current
        assert task["watchlist_input"] == \
            f"{EXP_ROOT}/{EXP_ID}/state/watchlist/judge/history/2026-10-27.yaml"

    def test_prepare_requires_previous_day_snapshot(self) -> None:
        """没有 T-1 冻结快照时必须报错，绝不回退 current。"""
        base = WS.path("temp/_test_no_history/state/watchlist/judge/current.yaml")
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_text("version: w0.0\nstocks: []\n", encoding="utf-8")
        e = Experiment(EXP_ID, WS, RunLogger(log_path=LOG_PATH),
                       exp_root=EXP_ROOT, watchlist_base=str(base))
        try:
            e.setup()
            with pytest.raises(WorkspaceError, match="不能回退 current"):
                e.prepare(DATE)
        finally:
            shutil.rmtree(WS.path(EXP_ROOT), ignore_errors=True)
            shutil.rmtree(WS.path("temp/_test_no_history"), ignore_errors=True)
            LOG_PATH.unlink(missing_ok=True)


class TestRun:
    def test_run_advances_and_waits_for_llm(self, exp: Experiment) -> None:
        exp.setup()
        exp.run(DAY, DAY)  # 无 review：自动 prepare 后列待办
        assert WS.path(f"{EXP_ROOT}/{EXP_ID}/runs/{DATE}/handoff/judge_task.yaml") \
            .is_file()

    def test_run_idempotent(self, exp: Experiment) -> None:
        exp.setup()
        exp.run(DAY, DAY)
        task = WS.path(
            f"{EXP_ROOT}/{EXP_ID}/runs/{DATE}/handoff/judge_task.yaml")
        first = task.read_bytes()
        exp.run(DAY, DAY)  # 重跑不改动
        assert task.read_bytes() == first

    def test_run_full_chain(self, exp: Experiment) -> None:
        exp.setup()
        exp.prepare(DATE)
        _write_review(exp.run_root)
        exp.run(DAY, DAY)  # 有 review：自动 continue → trader 任务包
        assert WS.path(
            f"{EXP_ROOT}/{EXP_ID}/runs/{DATE}/handoff/trader_task.yaml").is_file()

    def test_run_no_snapshots(self, exp: Experiment,
                              capsys: pytest.CaptureFixture) -> None:
        exp.setup()
        exp.run("20260101", "20260102")
        assert "无快照" in capsys.readouterr().out
