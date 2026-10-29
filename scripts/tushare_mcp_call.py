"""Call one read-only Tushare MCP tool and save its sanitized JSON response.

Usage: TUSHARE_TOKEN=... python scripts/tushare_mcp_call.py TOOL OUTPUT_JSON \
  '{"trade_date":"20260930","fields":["ts_code","trade_date","close"]}'
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import requests


def decode_response(response: requests.Response) -> Any:
    response.raise_for_status()
    text = response.content.decode("utf-8")
    if "text/event-stream" not in response.headers.get("content-type", ""):
        return json.loads(text)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    events = []
    for raw_event in normalized.split("\n\n"):
        payload = "\n".join(
            line[5:].lstrip() for line in raw_event.splitlines() if line.startswith("data:")
        ).strip()
        if payload and payload != "[DONE]":
            events.append(json.loads(payload))
    return events


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit("Expected: TOOL OUTPUT_JSON PARAMS_JSON")
    tool, output_name, params_text = sys.argv[1:]
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        raise SystemExit("TUSHARE_TOKEN is required")
    params = json.loads(params_text)
    response = requests.post(
        "https://api.tushare.pro/mcp/?token=" + token,
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": tool, "arguments": params
        }},
        timeout=120,
    )
    body = decode_response(response)
    output = Path(output_name)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    serialized = json.dumps(body, ensure_ascii=False)
    print(json.dumps({
        "tool": tool,
        "output": str(output),
        "response_chars": len(serialized),
        "preview": serialized[:1200],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
