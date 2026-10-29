# -*- coding: utf-8 -*-
"""Append-only JSONL run events; no Operator, server, or database required."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
LOG_PATH = ROOT / "logs" / "run_events.jsonl"


def emit_event(*, run_id: str, module: str, event: str, status: str,
               as_of: str | None = None, experiment_id: str | None = None,
               arm_id: str | None = None, duration_ms: int | None = None,
               input_tokens: int | None = None, output_tokens: int | None = None,
               cost: float | None = None, query_count: int | None = None,
               artifact_path: str | None = None, error: str | None = None,
               **extra: Any) -> dict:
    payload = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "run_id": run_id, "experiment_id": experiment_id, "arm_id": arm_id,
        "as_of": as_of, "module": module, "event": event, "status": status,
        "duration_ms": duration_ms, "input_tokens": input_tokens,
        "output_tokens": output_tokens, "cost": cost, "query_count": query_count,
        "artifact_path": artifact_path, "error": error,
    }
    payload.update(extra)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)
    return payload
