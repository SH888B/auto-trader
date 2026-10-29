# -*- coding: utf-8 -*-
"""Trader 两阶段交接协议：prepare() 出任务包，collect() 校验订单意图。

与 Judge 同构，差异在输入：Trader 任务包额外携带 Judge review 引用、
账户状态引用和受控查询白名单（theme_members/daily_bars/snapshot_table）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from .core import (
    WORKSPACE_ROOT,
    RunLogger,
    Workspace,
    WorkspaceError,
    load_yaml,
)
from .judge import _dash, _ref
from .pipeline.validate import require_ok, validate_order_intents

DEFAULT_PROFILE = "agents/trader/profiles/t1_first_divergence.yaml"
DEFAULT_PORTFOLIO = "state/portfolio/current.yaml"
DEFAULT_RUN_ROOT = "runs/live"
TRADER_VIEW = "configs/digest/trader_initial.yaml"


class Trader:
    """Trader Profile 的装配与验收。构造函数注入，无全局状态。"""

    def __init__(self, workspace: Workspace | None = None,
                 logger: RunLogger | None = None,
                 profile_path: str = DEFAULT_PROFILE,
                 run_root: str = DEFAULT_RUN_ROOT,
                 portfolio_path: str = DEFAULT_PORTFOLIO) -> None:
        self.ws = workspace or Workspace()
        self.logger = logger or RunLogger()
        self.profile_path = profile_path
        self.run_root = run_root
        self.portfolio_path = portfolio_path

    # ------------------------------------------------------------------ #
    # prepare：写 handoff/trader_task.yaml + trader_task.md
    # ------------------------------------------------------------------ #

    def prepare(self, as_of: str, run_id: str | None = None) -> Path:
        """装配 Trader 输入并写出任务包。幂等：产物内容不含时间戳，重跑字节一致。"""
        date_dash = _dash(as_of)
        day = date_dash.replace("-", "")
        rid = run_id or f"live-{day}-trader"
        run_dir = self.ws.path(self.run_root) / date_dash
        digest_rel = f"{self.run_root}/{date_dash}/trader/digest.md"
        review_rel = f"{self.run_root}/{date_dash}/judge/review.json"

        profile = load_yaml(self.ws.path(self.profile_path))
        if not profile.get("prompt"):
            raise WorkspaceError(f"Trader profile 缺少 prompt 字段：{self.profile_path}")

        inputs: list[dict[str, Any]] = [_ref(self.ws, self.profile_path),
                                        _ref(self.ws, profile["prompt"]),
                                        _ref(self.ws, profile["output_contract"]),
                                        _ref(self.ws, review_rel),
                                        _ref(self.ws, self.portfolio_path)]
        registry = load_yaml(self.ws.path("factors/registry.yaml"))
        known = {f"{f['id']}@{f['version']}": f
                 for f in registry.get("factors", [])}
        factor_files: list[dict[str, str]] = []
        for fid in profile.get("factors", []):
            entry = known.get(fid)
            if not entry or not entry.get("file"):
                raise WorkspaceError(f"profile 引用的因子未登记：{fid}")
            inputs.append(_ref(self.ws, entry["file"]))
            factor_files.append({"id": fid, "name": entry.get("name", ""),
                                 "file": entry["file"]})

        task = {
            "task_version": "ht1.0",
            "task": "trader",
            "run_id": rid,
            "as_of": date_dash,
            "profile": {"path": self.profile_path,
                        "id": profile.get("id"), "version": profile.get("version")},
            "prompt": profile["prompt"],
            "output_contract": profile["output_contract"],
            "digest": _ref(self.ws, digest_rel),
            "judge_review": _ref(self.ws, review_rel),
            "account_state": _ref(self.ws, self.portfolio_path),
            "factors": factor_files,
            "data_requirements": profile.get("data_requirements", []),
            "allowed_queries": profile.get("queries", []),
            "user_watchlist": profile.get("user_watchlist", "disabled"),
            "outputs": {
                "order_intents":
                    f"{self.run_root}/{date_dash}/arms/manual/trader/order_intents.json",
            },
            "inputs": inputs,
        }
        handoff = run_dir / "handoff"
        handoff.mkdir(parents=True, exist_ok=True)
        task_path = handoff / "trader_task.yaml"
        task_path.write_text(yaml.safe_dump(task, allow_unicode=True, sort_keys=False),
                             encoding="utf-8")
        (handoff / "trader_task.md").write_text(
            self._render_task_md(task, digest_rel, review_rel), encoding="utf-8")
        print(f"[handoff] {task_path}")
        return handoff

    def _render_task_md(self, task: dict, digest_rel: str, review_rel: str) -> str:
        lines = [
            f"# Trader 任务包 · {task['as_of']}",
            "",
            f"> run_id: {task['run_id']} ｜ profile: {task['profile']['id']} "
            f"{task['profile']['version']} ｜ 契约: {task['output_contract']}",
            "",
            "## 输入清单（hash 冻结，改动即失效）",
            "",
            "| 文件 | sha256 |",
            "|---|---|",
        ]
        for item in task["inputs"]:
            lines.append(f"| {item['path']} | {item['sha256'][:16]}… |")
        lines += [
            "",
            "## 操作要求",
            "",
            "1. 通读下方内嵌 Trader View 与 Judge review；因子文件按需读。",
            f"2. 数据只允许经受控查询获取：{('、'.join(task['allowed_queries'])) or '无'}。"
            "对 Judge 预判的明日首分板块，必须调用 get_board_momentum"
            "（board_codes 传全部预判板块、top_n=20）作为择股与挂价主输入。",
            f"3. 输出 order_intents.json 到 `{task['outputs']['order_intents']}`。",
            "4. 意图必须满足：buy 有挂价/止损/止盈且 stop < price < take；"
            "sell/hold 引用 position_state_ref 可校验的持仓；"
            "buy 合计 ≤ 1.0；空仓必须填 no_trade_reason。",
            "",
            "## Trader View（内嵌）",
            "",
            self.ws.path(digest_rel).read_text(encoding="utf-8"),
            "",
            "## Judge review（内嵌，同 judge_review hash 冻结）",
            "",
            "```json",
            self.ws.path(review_rel).read_text(encoding="utf-8"),
            "```",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # collect：校验 order_intents.json
    # ------------------------------------------------------------------ #

    def collect(self, as_of: str, run_id: str | None = None) -> dict:
        """校验落盘的 order_intents.json（schema + 引用 hash + 三价 + 持仓）。"""
        date_dash = _dash(as_of)
        day = date_dash.replace("-", "")
        rid = run_id or f"live-{day}-trader"
        common = {"run_id": rid, "module": "trader", "as_of": date_dash}
        intents_path = self.ws.path(
            f"{self.run_root}/{date_dash}/arms/manual/trader/order_intents.json")
        if not intents_path.is_file():
            raise WorkspaceError(
                f"缺少 {intents_path}：请先执行 handoff 任务包并落盘 order_intents.json")
        data = json.loads(intents_path.read_text(encoding="utf-8"))

        self.logger.emit(event="start", status="running", **common)
        try:
            require_ok(validate_order_intents(data, self.ws),
                       f"trader order_intents {date_dash}")
        except Exception as exc:
            self.logger.emit(event="error", status="failed", error=str(exc)[:200],
                             **common)
            raise
        self.logger.emit(event="end", status="success",
                         artifact_path=str(intents_path.relative_to(WORKSPACE_ROOT)),
                         **common)
        return data


__all__ = ["Trader"]
