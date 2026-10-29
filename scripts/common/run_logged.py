# -*- coding: utf-8 -*-
"""Run one command with start/end/error events in logs/run_events.jsonl.

Example:
    uv run python scripts/common/run_logged.py --run-id live-20261027-judge \
      --module judge --as-of 2026-10-27 -- \
      uv run python scripts/curator/render_digest.py --date 20261027
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from run_logger import emit_event

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description="带 JSONL 日志运行命令")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--module", required=True,
                        choices=["scout", "curator", "judge", "trader", "broker", "evaluator"])
    parser.add_argument("--as-of")
    parser.add_argument("--experiment-id")
    parser.add_argument("--arm-id")
    parser.add_argument("--artifact-path")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if not command:
        raise SystemExit("缺少要执行的命令")
    common = {k: v for k, v in {
        "run_id": args.run_id, "module": args.module, "as_of": args.as_of,
        "experiment_id": args.experiment_id, "arm_id": args.arm_id,
        "artifact_path": args.artifact_path,
    }.items() if v is not None}
    emit_event(event="start", status="running", **common)
    started = time.monotonic()
    try:
        result = subprocess.run(command, cwd=ROOT)
        duration = int((time.monotonic() - started) * 1000)
        if result.returncode == 0:
            emit_event(event="end", status="success", duration_ms=duration, **common)
        else:
            emit_event(event="error", status="failed", duration_ms=duration,
                       error=f"exit_code={result.returncode}", **common)
        raise SystemExit(result.returncode)
    except FileNotFoundError as exc:
        duration = int((time.monotonic() - started) * 1000)
        emit_event(event="error", status="failed", duration_ms=duration, error=str(exc), **common)
        raise
    except KeyboardInterrupt:
        duration = int((time.monotonic() - started) * 1000)
        emit_event(event="error", status="cancelled", duration_ms=duration,
                   error="KeyboardInterrupt", **common)
        raise


if __name__ == "__main__":
    main()
