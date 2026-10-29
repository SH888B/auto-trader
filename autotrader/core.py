"""auto-trader 基础设施：工作区、哈希、运行日志、状态存储、契约校验。

收拢自 scripts/common/（hashutil / run_logger / run_logged / state_store /
validate_contract），并统一四处重复的 sha256 实现。各业务模块（Scout、
Curator、Judge……）只依赖本文件提供的 API，不再各自维护工具函数。

代码规范：PEP 8（命名、布局）+ Google Python Style Guide（docstring、
类型注解、导入分组）；行长 100；源码默认 UTF-8，不写 coding 声明。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence

import yaml
from jsonschema import Draft7Validator

#: 工作区根目录（autotrader/ 的上一级），所有相对路径以此为锚。
WORKSPACE_ROOT = Path(__file__).resolve().parents[1]


class WorkspaceError(RuntimeError):
    """工作区路径、状态或契约违反时的统一异常。"""


# ---------------------------------------------------------------------------
# 哈希与工件引用
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    """计算文件的 SHA-256（1 MiB 分块，避免大文件占内存）。

    Args:
        path: 待哈希文件，必须存在。

    Returns:
        64 位十六进制摘要。

    Raises:
        WorkspaceError: 文件不存在。
    """
    path = Path(path)
    if not path.is_file():
        raise WorkspaceError(f"文件不存在：{path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Workspace:
    """工作区：所有模块的文件操作都必须经由此类解析路径。

    职责：
        - 把相对路径解析为绝对路径，并强制落在工作区内（防越界读写）；
        - 生成带 hash 的工件引用（artifact_ref），供 manifest / snapshot 冻结。
    """

    root: Path = WORKSPACE_ROOT

    def path(self, value: str | Path) -> Path:
        """解析为工作区内的绝对路径。

        Raises:
            WorkspaceError: 解析结果落在工作区之外。
        """
        candidate = Path(value)
        resolved = (self.root / candidate).resolve() if not candidate.is_absolute() \
            else candidate.resolve()
        if not resolved.is_relative_to(self.root.resolve()):
            raise WorkspaceError(f"路径必须位于工作区内：{resolved}")
        return resolved

    def artifact_ref(self, path: str | Path) -> dict[str, str]:
        """生成工件引用：``{"path": 工作区相对路径, "sha256": 摘要}``。"""
        resolved = self.path(path)
        return {
            "path": resolved.relative_to(self.root.resolve()).as_posix(),
            "sha256": sha256_file(resolved),
        }


# ---------------------------------------------------------------------------
# 配置与数据文件读写
# ---------------------------------------------------------------------------

def load_yaml(path: str | Path) -> dict[str, Any]:
    """读取 YAML 文件；文件不存在时返回空 dict。"""
    target = Path(path)
    if not target.is_file():
        return {}
    return yaml.safe_load(target.read_text(encoding="utf-8")) or {}


def load_json(path: str | Path) -> Any:
    """读取 JSON 文件。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def dump_yaml(data: Any) -> str:
    """按工作区惯例序列化 YAML（Unicode 原样、保持键序）。"""
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


# ---------------------------------------------------------------------------
# 日期规范：对外唯一格式 YYYYMMDD（如 20261028）
# ---------------------------------------------------------------------------

def trade_date(value: str | date | datetime) -> str:
    """把日期统一成 ``YYYYMMDD`` 字符串；非法输入抛 WorkspaceError。

    这是 V5 起的唯一对外格式：CLI 参数、目录名、raw 文件名、库里 trade_date
    列全部一致，不再混用 ``2026-10-28``。
    """
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    text = str(value).strip().replace("-", "").replace("/", "")
    if len(text) != 8 or not text.isdigit():
        raise WorkspaceError(f"trade_date 必须是 YYYYMMDD：{value!r}")
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError as exc:
        raise WorkspaceError(f"trade_date 不是合法日期：{value!r}") from exc
    return text


# ---------------------------------------------------------------------------
# 运行日志（append-only JSONL）
# ---------------------------------------------------------------------------

@dataclass
class RunLogger:
    """向 ``logs/run_events.jsonl`` 追加结构化运行事件。

    只追加、不改写；即使调用方崩溃，已写入行也保持完整（单行原子写入）。
    """

    log_path: Path = field(default=WORKSPACE_ROOT / "logs" / "run_events.jsonl")

    def emit(self, *, run_id: str, module: str, event: str, status: str,
             as_of: str | None = None, experiment_id: str | None = None,
             arm_id: str | None = None, duration_ms: int | None = None,
             input_tokens: int | None = None, output_tokens: int | None = None,
             cost: float | None = None, query_count: int | None = None,
             artifact_path: str | None = None, error: str | None = None,
             **extra: Any) -> dict[str, Any]:
        """追加一条事件并返回写入的 payload。

        Args:
            run_id: 运行唯一标识。
            module: 模块名（scout/curator/judge/trader/broker/evaluator）。
            event: start / end / error。
            status: running / success / failed / cancelled。
            **extra: 附加字段原样进入 payload。
        """
        payload: dict[str, Any] = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "run_id": run_id, "experiment_id": experiment_id, "arm_id": arm_id,
            "as_of": as_of, "module": module, "event": event, "status": status,
            "duration_ms": duration_ms, "input_tokens": input_tokens,
            "output_tokens": output_tokens, "cost": cost, "query_count": query_count,
            "artifact_path": artifact_path, "error": error,
        }
        payload.update(extra)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        return payload

    def run_command(self, command: Sequence[str], *, run_id: str, module: str,
                    as_of: str | None = None, experiment_id: str | None = None,
                    arm_id: str | None = None, artifact_path: str | None = None,
                    cwd: Path | None = None) -> int:
        """以 start/end/error 事件包裹一条子命令执行（收拢 run_logged.py）。

        Args:
            command: 子命令 argv。
            cwd: 子进程工作目录，默认工作区根。

        Returns:
            子进程退出码（调用方据此决定流程去向）。
        """
        common = {"run_id": run_id, "module": module, "as_of": as_of,
                  "experiment_id": experiment_id, "arm_id": arm_id,
                  "artifact_path": artifact_path}
        self.emit(event="start", status="running", **common)
        started = datetime.now().timestamp()
        try:
            result = subprocess.run(list(command), cwd=cwd or WORKSPACE_ROOT)
        except FileNotFoundError as exc:
            duration = int((datetime.now().timestamp() - started) * 1000)
            self.emit(event="error", status="failed", duration_ms=duration,
                      error=str(exc), **common)
            raise
        except KeyboardInterrupt:
            duration = int((datetime.now().timestamp() - started) * 1000)
            self.emit(event="error", status="cancelled", duration_ms=duration,
                      error="KeyboardInterrupt", **common)
            raise
        duration = int((datetime.now().timestamp() - started) * 1000)
        if result.returncode == 0:
            self.emit(event="end", status="success", duration_ms=duration, **common)
        else:
            self.emit(event="error", status="failed", duration_ms=duration,
                      error=f"exit_code={result.returncode}", **common)
        return result.returncode


# ---------------------------------------------------------------------------
# 状态存储（current + 只追加 history 快照）
# ---------------------------------------------------------------------------

@dataclass
class StateStore:
    """按天演化的状态存储：judge/user watchlist 与 portfolio 共用。

    语义：
        - ``current.yaml`` 是"最新收盘状态"缓存视图；
        - ``history/<date>.yaml`` 是只追加的每日快照，代表"该日收盘后"状态；
        - as_of 查询永远回退 history，不读未冻结的 current；
        - history 快照已存在且内容不同 → 拒绝覆盖（防篡改/防重放错乱）。
    """

    workspace: Workspace = field(default_factory=Workspace)

    def _resolve(self, target: str | Path) -> Path:
        return self.workspace.path(target)

    def load(self, target: str | Path) -> dict[str, Any]:
        """读取状态文件；不存在返回空 dict。"""
        return load_yaml(self._resolve(target))

    def as_of_path(self, target: str | Path, as_of: str,
                   history_dir: str | Path | None = None,
                   *, strict: str = "not_after") -> Path | None:
        """返回 as_of 时点的状态文件路径（history 回退查询）；无则 None。

        Args:
            strict: "not_after"（默认，<= as_of 的最新一份）/
                "before"（严格 < as_of，用于 Judge 输入：T 日只能看 T-1）/
                "exact"（必须正好是 as_of 当天）。
        """
        resolved = self._resolve(target)
        history = self._resolve(history_dir) if history_dir else resolved.parent / "history"
        if not history.is_dir():
            return None
        if strict == "exact":
            candidate = history / f"{as_of}.yaml"
            return candidate if candidate.is_file() else None
        if strict not in ("before", "not_after"):
            raise WorkspaceError(f"未知 strict 模式：{strict}")
        # 用文件名比较（而非 stem）：变体名 <date>-w1.0.yaml 会排在规范名 <date>.yaml 之前
        candidates = sorted(p for p in history.glob("*.yaml")
                            if (p.name < f"{as_of}.yaml" if strict == "before"
                                else p.name <= f"{as_of}.yaml"))
        return candidates[-1] if candidates else None

    def load_as_of(self, target: str | Path, as_of: str,
                   history_dir: str | Path | None = None,
                   *, strict: str = "not_after") -> dict[str, Any]:
        """读取 as_of 时点的冻结状态；无快照返回空 dict。"""
        snapshot = self.as_of_path(target, as_of, history_dir, strict=strict)
        return load_yaml(snapshot) if snapshot else {}

    def freeze(self, target: str | Path, snapshot_date: str,
               history_dir: str | Path | None = None) -> Path:
        """把 current 原样冻结为 ``history/<date>.yaml``（不重写 current）。

        Raises:
            WorkspaceError: current 缺失，或历史快照已存在且内容不同。
        """
        resolved = self._resolve(target)
        if not resolved.is_file():
            raise WorkspaceError(f"状态文件不存在，无法冻结：{resolved}")
        rendered = resolved.read_text(encoding="utf-8")
        history = self._resolve(history_dir) if history_dir else resolved.parent / "history"
        snapshot = history / f"{snapshot_date}.yaml"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        if snapshot.exists() and snapshot.read_text(encoding="utf-8") != rendered:
            raise WorkspaceError(f"历史快照已存在且内容不同，拒绝覆盖：{snapshot}")
        if not snapshot.exists():
            snapshot.write_text(rendered, encoding="utf-8")
        return snapshot

    def save(self, target: str | Path, state: dict[str, Any],
             snapshot_date: str | None = None,
             history_dir: str | Path | None = None) -> Path | None:
        """写 current + 可选每日快照；快照冲突拒绝覆盖。返回快照路径。

        Raises:
            WorkspaceError: 历史快照已存在且内容不同。
        """
        resolved = self._resolve(target)
        rendered = dump_yaml(state)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(rendered, encoding="utf-8")
        if snapshot_date is None:
            return None
        history = self._resolve(history_dir) if history_dir else resolved.parent / "history"
        snapshot = history / f"{snapshot_date}.yaml"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        if snapshot.exists() and snapshot.read_text(encoding="utf-8") != rendered:
            raise WorkspaceError(f"历史快照已存在且内容不同，拒绝覆盖：{snapshot}")
        if not snapshot.exists():
            snapshot.write_text(rendered, encoding="utf-8")
        return snapshot


# ---------------------------------------------------------------------------
# 契约校验（JSON Schema）
# ---------------------------------------------------------------------------

def validate_against_schema(instance: Any, schema: dict[str, Any]) -> list[str]:
    """校验实例，返回按路径排序的错误消息列表（空列表 = 通过）。"""
    errors = sorted(Draft7Validator(schema).iter_errors(instance),
                    key=lambda e: list(e.path))
    messages = []
    for error in errors:
        location = ".".join(str(p) for p in error.path) or "<root>"
        messages.append(f"{location}: {error.message}")
    return messages


def validate_contract(instance_path: str | Path,
                      schema_path: str | Path) -> list[str]:
    """校验 JSON/YAML 工件 against 契约 schema（收拢 validate_contract.py）。

    Args:
        instance_path: 待校验文件（.json/.yaml/.yml）。
        schema_path: contracts/ 下的 JSON Schema。
    """
    target = Path(instance_path)
    if target.suffix.lower() in {".yaml", ".yml"}:
        instance = load_yaml(target)
    else:
        instance = load_json(target)
    schema = load_json(schema_path)
    return validate_against_schema(instance, schema)
