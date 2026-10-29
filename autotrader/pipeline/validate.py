# -*- coding: utf-8 -*-
"""契约与语义校验（原 scripts/judge/validate_review.py + scripts/trader/
validate_order_intents.py）。

统一返回 ValidationResult，由调用方决定抛错或仅告警；CLI 薄壳负责以退出码呈现。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from jsonschema import Draft7Validator

from ..core import WORKSPACE_ROOT, Workspace, WorkspaceError, sha256_file

REVIEW_SCHEMA = WORKSPACE_ROOT / "contracts" / "review.schema.json"
INTENTS_SCHEMA = WORKSPACE_ROOT / "contracts" / "order_intents.schema.json"


@dataclass
class ValidationResult:
    """校验结果：errors 非空即失败；warnings 仅提示。"""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _schema_errors(instance: dict, schema_path: Path) -> list[str]:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return sorted(
        (f"{'.'.join(map(str, e.path)) or '<root>'}: {e.message}"
         for e in Draft7Validator(schema).iter_errors(instance)),
        key=str,
    )


# --------------------------------------------------------------------------- #
# Judge review（原 validate_review.py，逻辑逐行平移）
# --------------------------------------------------------------------------- #

def validate_review(review: dict) -> ValidationResult:
    """schema + v1.1/v1.2 结构化要求 + 主题引用完整性。"""
    result = ValidationResult()
    result.errors.extend(_schema_errors(review, REVIEW_SCHEMA))

    version = review.get("review_version", "v1")
    themes = review.get("themes", []) or []
    carriers = review.get("carriers", []) or []
    theme_ids = {t.get("theme_id") for t in themes}
    regime = review.get("market_regime", {}) or {}

    def check_refs(refs: list, owner: str, field: str) -> None:
        for r in refs or []:
            if r not in theme_ids:
                msg = f"{owner}.{field}: 引用了不存在的 theme_id '{r}'"
                (result.errors if version in ("v1.1", "v1.2")
                 else result.warnings).append(msg)

    structured = version in ("v1.1", "v1.2")
    if structured:
        if regime.get("label"):
            result.warnings.append(
                "market_regime.label 为 v1.1 弃用字段，不应再写自由文本 label")
        for i, t in enumerate(themes):
            tid = t.get("theme_id", f"#{i}")
            if t.get("stage"):
                result.warnings.append(
                    f"themes[{tid}].stage 为 v1.1 弃用字段，改用 rhythm_stage")
            if not t.get("rhythm_stage"):
                result.errors.append(f"themes[{tid}]: v1.1 产物缺少 rhythm_stage")
            if t.get("rhythm_stage") == "首分":
                basis = (t.get("stage_basis") or "").strip()
                if len(basis) < 10:
                    result.errors.append(
                        f"themes[{tid}]: 首分缺少有效的 stage_basis（三天组合K依据）")
                elif not re.search(r"阳线|大阳|涨", basis):
                    result.warnings.append(
                        f"themes[{tid}]: stage_basis 未提及阳线组合，请核对首分严格定义")
        for i, c in enumerate(carriers):
            cid = c.get("ts_code", f"#{i}")
            if c.get("role"):
                result.warnings.append(
                    f"carriers[{cid}].role 为 v1.1 弃用字段，改用 carrier_class")
            if not c.get("carrier_class"):
                result.errors.append(f"carriers[{cid}]: v1.1 产物缺少 carrier_class")
            if not c.get("capacity"):
                result.warnings.append(f"carriers[{cid}]: 未标注 capacity（成交额量级）")

    if version == "v1.2":
        fd_boards = review.get("next_day_first_divergence_boards") or []
        for i, fb in enumerate(fd_boards):
            code = fb.get("board_code", f"#{i}")
            check_refs([fb.get("theme_id")],
                       f"next_day_first_divergence_boards[{code}]", "theme_id")
            basis = (fb.get("basis") or "").strip()
            if len(basis) < 10:
                result.errors.append(
                    f"next_day_first_divergence_boards[{code}]: 预判依据 basis 过短（<10 字），"
                    "须写明 stage/位置/量能/分歧迹象")

    for i, c in enumerate(carriers):
        cid = c.get("ts_code", f"#{i}")
        check_refs([c.get("theme_id")], f"carriers[{cid}]", "theme_id")
    for i, t in enumerate(themes):
        tid = t.get("theme_id", f"#{i}")
        check_refs(t.get("competitors"), f"themes[{tid}]", "competitors")
    check_refs(regime.get("switch_day", {}).get("candidates"),
               "market_regime.switch_day", "candidates")

    return result


# --------------------------------------------------------------------------- #
# Trader order_intents（原 validate_order_intents.py，逻辑逐行平移）
# --------------------------------------------------------------------------- #

def _load_positions(data: dict, ws: Workspace, errors: list[str]) -> dict[str, float]:
    """v1.1：position_state_ref 存在时加载 T 日收盘持仓 {ts_code: open_pct}。"""
    ref = data.get("position_state_ref") or {}
    artifact = ws.path(ref.get("path", ""))
    if not artifact.is_file():
        errors.append(f"position_state_ref: 文件不存在 {artifact}")
        return {}
    if sha256_file(artifact) != ref.get("sha256"):
        errors.append(f"position_state_ref: hash 不匹配 {artifact}")
        return {}
    state = yaml.safe_load(artifact.read_text(encoding="utf-8")) or {}
    holdings: dict[str, float] = {}
    for pos in state.get("positions", []):
        if pos.get("status") == "open":
            holdings[pos["ts_code"]] = holdings.get(pos["ts_code"], 0.0) + float(
                pos.get("pct", 0))
    return holdings


def validate_order_intents(data: dict, ws: Workspace | None = None) -> ValidationResult:
    """schema + 引用 hash + 三价关系 + 持仓一致性。"""
    ws = ws or Workspace()
    result = ValidationResult()
    result.errors.extend(_schema_errors(data, INTENTS_SCHEMA))

    for key in ("judge_review_ref", "account_state_ref", "query_manifest_ref"):
        ref = data.get(key) or {}
        artifact = ws.path(ref.get("path", ""))
        if not artifact.is_file():
            result.errors.append(f"{key}: 文件不存在 {artifact}")
        elif sha256_file(artifact) != ref.get("sha256"):
            result.errors.append(f"{key}: hash 不匹配 {artifact}")

    holdings = (_load_positions(data, ws, result.errors)
                if data.get("position_state_ref") else {})

    total_buy = 0.0
    for i, action in enumerate(data.get("actions", [])):
        kind = action.get("action")
        price, stop, take = (action.get("limit_price"), action.get("stop_loss"),
                             action.get("take_profit"))
        pct = float(action.get("position_pct", 0))
        if kind == "buy":
            total_buy += pct
            if not all(isinstance(v, (int, float)) for v in (price, stop, take)):
                result.errors.append(f"actions[{i}]: buy 必须有挂价、止损、止盈")
            elif not stop < price < take:
                result.errors.append(
                    f"actions[{i}]: 必须 stop_loss < limit_price < take_profit")
        if kind in ("sell", "hold"):
            if not holdings:
                result.errors.append(
                    f"actions[{i}]: {kind} 单必须提供可校验的 position_state_ref 持仓")
            elif action.get("ts_code") not in holdings:
                result.errors.append(
                    f"actions[{i}]: {kind} 标的 {action.get('ts_code')} "
                    f"不在持仓中 {sorted(holdings)}")
            elif kind == "hold" and pct > holdings[action["ts_code"]] + 1e-9:
                result.errors.append(
                    f"actions[{i}]: hold 仓位 {pct} 超过持仓 {holdings[action['ts_code']]}")
            # sell 的 position_pct 语义为"卖出该持仓的比例"（1.0=清仓），不与账户占比比较
        if kind == "cancel" and action.get("order_type") != "none":
            result.errors.append(f"actions[{i}]: cancel 的 order_type 必须为 none")
    if total_buy > 1.0:
        result.errors.append(f"buy 仓位合计 {total_buy:.2f} > 1.0")
    if holdings and total_buy + sum(holdings.values()) > 1.0 + 1e-9:
        result.errors.append(
            f"买入后总仓位 {total_buy + sum(holdings.values()):.2f} > 1.0")
    if not data.get("actions") and not data.get("no_trade_reason"):
        result.errors.append("空仓时必须填写 no_trade_reason")

    return result


def require_ok(result: ValidationResult, label: str) -> None:
    """errors 非空时抛 WorkspaceError（collect 阶段的统一出口）。"""
    if not result.ok:
        raise WorkspaceError(f"{label} 校验失败：\n  - " + "\n  - ".join(result.errors))
