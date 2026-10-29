"""autotrader.core 单元测试：哈希、工作区、状态存储、契约校验、运行日志。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from autotrader.core import (
    RunLogger,
    StateStore,
    Workspace,
    WorkspaceError,
    sha256_file,
    validate_against_schema,
    validate_contract,
)

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / "temp" / "_test_core"

#: "abc" 的已知 SHA-256，验证哈希实现与标准一致。
SHA256_ABC = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


@pytest.fixture()
def sandbox() -> Path:
    """temp/_test_core 下的独立沙箱，测试结束清空。"""
    TMP.mkdir(parents=True, exist_ok=True)
    yield TMP
    for path in sorted(TMP.rglob("*"), reverse=True):
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            path.rmdir()


def test_sha256_matches_known_vector(sandbox: Path) -> None:
    target = sandbox / "abc.txt"
    target.write_text("abc", encoding="utf-8")
    assert sha256_file(target) == SHA256_ABC


def test_sha256_missing_file_raises(sandbox: Path) -> None:
    with pytest.raises(WorkspaceError):
        sha256_file(sandbox / "missing.txt")


def test_workspace_rejects_outside() -> None:
    with pytest.raises(WorkspaceError):
        Workspace().path("../outside.txt")


def test_artifact_ref_relative_and_hash(sandbox: Path) -> None:
    target = sandbox / "artifact.txt"
    target.write_text("abc", encoding="utf-8")
    ref = Workspace().artifact_ref(target)
    assert ref["path"] == "temp/_test_core/artifact.txt"
    assert ref["sha256"] == SHA256_ABC


def test_state_store_snapshot_refuses_overwrite(sandbox: Path) -> None:
    store = StateStore()
    current = sandbox / "state" / "current.yaml"
    store.save(current, {"level": 1}, snapshot_date="2026-01-01")
    with pytest.raises(WorkspaceError):
        store.save(current, {"level": 2}, snapshot_date="2026-01-01")
    # 内容相同则幂等通过
    store.save(current, {"level": 2}, snapshot_date="2026-01-02")


def test_state_store_load_as_of_falls_back_to_history(sandbox: Path) -> None:
    store = StateStore()
    current = sandbox / "wl" / "current.yaml"
    store.save(current, {"level": 1}, snapshot_date="2026-01-01")
    store.save(current, {"level": 2}, snapshot_date="2026-01-03")
    assert store.load_as_of(current, "2026-01-02") == {"level": 1}
    assert store.load_as_of(current, "2026-01-03") == {"level": 2}
    assert store.load_as_of(current, "2025-12-31") == {}


def test_state_store_strict_before_excludes_same_day(sandbox: Path) -> None:
    """Judge 输入必须严格早于 T：T 日自己的状态是 Judge 收盘后才产出的。"""
    store = StateStore()
    current = sandbox / "wl_before" / "current.yaml"
    store.save(current, {"level": 1}, snapshot_date="2026-10-27")
    assert store.as_of_path(current, "2026-10-28", strict="before").name == \
        "2026-10-27.yaml"
    assert store.as_of_path(current, "2026-10-27", strict="before") is None
    assert store.as_of_path(current, "2026-10-27", strict="exact").name == \
        "2026-10-27.yaml"


def test_state_store_variant_name_sorts_before_canonical(sandbox: Path) -> None:
    """<date>-w1.0.yaml 变体不应盖过规范 <date>.yaml。"""
    store = StateStore()
    current = sandbox / "wl_variant" / "current.yaml"
    history = current.parent / "history"
    history.mkdir(parents=True)
    (history / "2026-10-27.yaml").write_text("level: canonical\n", encoding="utf-8")
    (history / "2026-10-27-w1.0.yaml").write_text("level: variant\n", encoding="utf-8")
    assert store.as_of_path(current, "2026-10-28", strict="before").name == \
        "2026-10-27.yaml"


def test_state_store_freeze_copies_current_and_refuses_conflict(sandbox: Path) -> None:
    store = StateStore()
    current = sandbox / "wl_freeze" / "current.yaml"
    current.parent.mkdir(parents=True)
    current.write_text("stocks: []\n", encoding="utf-8")
    snap = store.freeze(current, "2026-10-28")
    assert snap.read_text(encoding="utf-8") == "stocks: []\n"
    # 幂等
    assert store.freeze(current, "2026-10-28") == snap
    current.write_text("stocks: [{ts_code: 000001.SZ}]\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="拒绝覆盖"):
        store.freeze(current, "2026-10-28")


def test_validate_against_schema_errors_sorted() -> None:
    schema = {"type": "object", "required": ["a", "b"]}
    errors = validate_against_schema({"b": 1}, schema)
    assert len(errors) == 1 and "a" in errors[0]
    assert validate_against_schema({"a": 1, "b": 2}, schema) == []


def test_validate_contract_accepts_yaml_and_json(sandbox: Path) -> None:
    schema_path = sandbox / "schema.json"
    schema_path.write_text(json.dumps({"type": "object", "required": ["a"]}),
                           encoding="utf-8")
    ok_yaml = sandbox / "ok.yaml"
    ok_yaml.write_text("a: 1\n", encoding="utf-8")
    bad_json = sandbox / "bad.json"
    bad_json.write_text("{}", encoding="utf-8")
    assert validate_contract(ok_yaml, schema_path) == []
    assert validate_contract(bad_json, schema_path)


def test_runlogger_emit_appends_jsonl(sandbox: Path) -> None:
    log_path = sandbox / "events.jsonl"
    logger = RunLogger(log_path=log_path)
    payload = logger.emit(run_id="t1", module="scout", event="end",
                          status="success", duration_ms=5, query_count=2)
    line = json.loads(log_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert line["run_id"] == "t1" and line["query_count"] == 2
    assert payload["event"] == "end"


def test_runlogger_wraps_command(sandbox: Path) -> None:
    log_path = sandbox / "cmd_events.jsonl"
    logger = RunLogger(log_path=log_path)
    ok = logger.run_command([sys.executable, "-c", "pass"],
                            run_id="t2", module="curator")
    assert ok == 0
    fail = logger.run_command([sys.executable, "-c", "raise SystemExit(3)"],
                              run_id="t3", module="judge")
    assert fail == 3
    lines = [json.loads(x) for x in log_path.read_text(encoding="utf-8").splitlines()]
    assert [(x["event"], x["status"]) for x in lines] == [
        ("start", "running"), ("end", "success"),
        ("start", "running"), ("error", "failed"),
    ]
    assert "exit_code=3" in lines[-1]["error"]
