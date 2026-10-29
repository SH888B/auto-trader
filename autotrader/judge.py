# -*- coding: utf-8 -*-
"""Judge 两阶段交接协议：prepare() 出任务包，collect() 校验落盘产物。

任务包（handoff/judge_task.yaml + judge_task.md）是 LLM 执行者的唯一入口：
输入清单 + hash 冻结 + 内嵌 Digest + 输出要求。执行者落盘 review.md/json 后，
collect() 做 schema + 语义校验，并按需应用 watchlist 更新。
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
    sha256_file,
)
from .curator import Curator
from .pipeline.validate import require_ok, validate_review

DEFAULT_PROFILE = "agents/judge/profiles/j5_default.yaml"
DEFAULT_WATCHLIST = "state/watchlist/judge/current.yaml"
DEFAULT_RUN_ROOT = "runs/live"


def _dash(date: str) -> str:
    """YYYYMMDD / YYYY-MM-DD 统一为 YYYY-MM-DD。"""
    return f"{date[:4]}-{date[4:6]}-{date[6:]}" if "-" not in date else date


def _ref(ws: Workspace, rel: str) -> dict[str, Any]:
    """构造 {path, sha256} 引用；文件缺失抛 WorkspaceError。"""
    path = ws.path(rel)
    if not path.is_file():
        raise WorkspaceError(f"任务包输入缺失：{rel}")
    return {"path": rel, "sha256": sha256_file(path)}


class Judge:
    """Judge Profile 的装配与验收。构造函数注入，无全局状态。"""

    def __init__(self, workspace: Workspace | None = None,
                 logger: RunLogger | None = None,
                 profile_path: str = DEFAULT_PROFILE,
                 run_root: str = DEFAULT_RUN_ROOT,
                 watchlist_target: str = DEFAULT_WATCHLIST) -> None:
        self.ws = workspace or Workspace()
        self.logger = logger or RunLogger()
        self.profile_path = profile_path
        self.run_root = run_root
        self.watchlist_target = watchlist_target

    # ------------------------------------------------------------------ #
    # prepare：写 handoff/judge_task.yaml + judge_task.md
    # ------------------------------------------------------------------ #

    def prepare(self, as_of: str, run_id: str | None = None) -> Path:
        """装配 Judge 输入并写出任务包。幂等：产物内容不含时间戳，重跑字节一致。

        Returns:
            任务包目录（<run_root>/<date>/handoff/）。
        """
        date_dash = _dash(as_of)
        day = date_dash.replace("-", "")
        rid = run_id or f"live-{day}-judge"
        run_dir = self.ws.path(self.run_root) / date_dash
        judge_dir = run_dir / "judge"
        digest_rel = f"{self.run_root}/{date_dash}/judge/digest.md"
        manifest_rel = f"{self.run_root}/{date_dash}/judge/input_manifest.yaml"

        profile = load_yaml(self.ws.path(self.profile_path))
        if not profile.get("prompt"):
            raise WorkspaceError(f"Judge profile 缺少 prompt 字段：{self.profile_path}")

        # 输入清单：profile/prompt/契约/因子/watchlist/历史 review
        inputs: list[dict[str, Any]] = [_ref(self.ws, self.profile_path),
                                        _ref(self.ws, profile["prompt"]),
                                        _ref(self.ws, profile["output_contract"])]
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
        inputs.append(_ref(self.ws, self.watchlist_target))
        if profile.get("user_watchlist") == "enabled":
            inputs.append(_ref(self.ws, "state/watchlist/user/current.yaml"))
        prev_review = self._previous_review(date_dash)
        if prev_review:
            inputs.append(_ref(self.ws, prev_review))

        digest_ref = _ref(self.ws, digest_rel)
        manifest_ref = _ref(self.ws, manifest_rel)
        task = {
            "task_version": "ht1.0",
            "task": "judge",
            "run_id": rid,
            "as_of": date_dash,
            "profile": {"path": self.profile_path,
                        "id": profile.get("id"), "version": profile.get("version")},
            "prompt": profile["prompt"],
            "output_contract": profile["output_contract"],
            "digest": digest_ref,
            "input_manifest": manifest_ref,
            "factors": factor_files,
            "previous_review": prev_review or None,
            "allowed_queries": profile.get("queries", []),
            "user_watchlist": profile.get("user_watchlist", "disabled"),
            "outputs": {
                "review_md": f"{self.run_root}/{date_dash}/judge/review.md",
                "review_json": f"{self.run_root}/{date_dash}/judge/review.json",
            },
            "inputs": inputs,
        }
        handoff = run_dir / "handoff"
        handoff.mkdir(parents=True, exist_ok=True)
        task_path = handoff / "judge_task.yaml"
        task_path.write_text(yaml.safe_dump(task, allow_unicode=True, sort_keys=False),
                             encoding="utf-8")
        (handoff / "judge_task.md").write_text(
            self._render_task_md(task, judge_dir, digest_rel), encoding="utf-8")
        print(f"[handoff] {task_path}")
        return handoff

    def _previous_review(self, date_dash: str) -> str | None:
        """找早于 as_of 的最近一份 judge review.json（相对 workspace 路径）。"""
        root = self.ws.path(self.run_root)
        if not root.is_dir():
            return None
        candidates = sorted(
            (d for d in root.iterdir()
             if d.is_dir() and d.name < date_dash
             and (d / "judge" / "review.json").is_file()),
            reverse=True)
        return f"{self.run_root}/{candidates[0].name}/judge/review.json" if candidates else None

    def _render_task_md(self, task: dict, judge_dir: Path, digest_rel: str) -> str:
        lines = [
            f"# Judge 任务包 · {task['as_of']}",
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
        prev = task["previous_review"] or "无（首日或缺失）"
        lines += [
            "",
            "## 操作要求",
            "",
            f"1. 通读下方内嵌 Digest；按需读因子文件与上一日 review（{prev}）。",
            f"2. 受控查询仅限：{('、'.join(task['allowed_queries'])) or '无'}"
            f"（`auto-trader curator query --request <task.yaml>`，须引用本包 digest hash）。",
            f"3. 输出 review.md 与 review.json 到 `{task['outputs']['review_json']}` 同级目录。",
            "4. review.json 必须符合 output_contract 的 schema，并满足语义要求："
            "v1.1+ themes 用 rhythm_stage、首分必须有 stage_basis、"
            "carriers 引用 theme_id 必须存在。",
            "5. 有 watchlist 调整时写 watchlist_updates，由 collect 阶段统一应用并冻结快照。",
            "",
            "## Digest（内嵌，同 input_manifest 冻结）",
            "",
            self.ws.path(digest_rel).read_text(encoding="utf-8"),
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # collect：校验 review 并应用 watchlist 更新
    # ------------------------------------------------------------------ #

    def collect(self, as_of: str, run_id: str | None = None) -> dict:
        """校验落盘的 review.json；有 watchlist_updates 时应用并冻结快照。"""
        date_dash = _dash(as_of)
        day = date_dash.replace("-", "")
        rid = run_id or f"live-{day}-judge"
        common = {"run_id": rid, "module": "judge", "as_of": date_dash}
        review_path = self.ws.path(
            f"{self.run_root}/{date_dash}/judge/review.json")
        if not review_path.is_file():
            raise WorkspaceError(
                f"缺少 {review_path}：请先执行 handoff 任务包并落盘 review.json")
        review = json.loads(review_path.read_text(encoding="utf-8"))

        self.logger.emit(event="start", status="running", **common)
        result = validate_review(review)
        for w in result.warnings:
            print(f"[警告] {w}")
        try:
            require_ok(result, f"judge review {date_dash}")
            updates = review.get("watchlist_updates") or []
            if updates:
                Curator(self.ws).apply_watchlist_updates(
                    str(review_path), self.watchlist_target)
        except Exception as exc:
            self.logger.emit(event="error", status="failed", error=str(exc)[:200],
                             **common)
            raise
        self.logger.emit(event="end", status="success",
                         artifact_path=str(review_path.relative_to(WORKSPACE_ROOT)),
                         **common)
        return review


__all__ = ["Judge"]
