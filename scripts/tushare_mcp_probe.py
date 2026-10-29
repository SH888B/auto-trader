"""Minimal, read-only Tushare MCP connectivity probe.

Requires TUSHARE_TOKEN in the environment. The token is never written to disk
or printed. This script only initializes the MCP session and lists tools.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "probe"
OUT.mkdir(parents=True, exist_ok=True)
TOKEN = os.environ.get("TUSHARE_TOKEN")
if not TOKEN:
    raise SystemExit("TUSHARE_TOKEN is required")

URL = "https://api.tushare.pro/mcp/?token=" + TOKEN
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def decode_response(response: requests.Response) -> Any:
    text = response.content.decode("utf-8")
    (OUT / "mcp_last_response_raw.txt").write_text(text, encoding="utf-8")
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        events = []
        # This server uses CR CR between events; normalize all newline styles.
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        for raw_event in normalized.split("\n\n"):
            payload = "\n".join(
                line[5:].lstrip()
                for line in raw_event.splitlines()
                if line.startswith("data:")
            ).strip()
            if payload and payload != "[DONE]":
                events.append(json.loads(payload))
        return events
    return json.loads(text)


def main() -> None:
    with requests.Session() as session:
        init = session.post(
            URL,
            headers=HEADERS,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "shoufen-backtest-probe", "version": "0.1"},
                },
            },
            timeout=45,
        )
        init.raise_for_status()
        session_id = init.headers.get("mcp-session-id")
        if session_id:
            HEADERS["mcp-session-id"] = session_id
        init_body = decode_response(init)

        initialized = session.post(
            URL,
            headers=HEADERS,
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            timeout=45,
        )
        initialized.raise_for_status()

        listed = session.post(
            URL,
            headers=HEADERS,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            timeout=60,
        )
        listed.raise_for_status()
        tools_body = decode_response(listed)

    # Save protocol output only; the URL and token are deliberately excluded.
    (OUT / "mcp_initialize.json").write_text(
        json.dumps(init_body, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT / "mcp_tools.json").write_text(
        json.dumps(tools_body, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    bodies = tools_body if isinstance(tools_body, list) else [tools_body]
    tools = []
    for body in bodies:
        if isinstance(body, dict):
            tools.extend(body.get("result", {}).get("tools", []))
    names = [tool.get("name", "") for tool in tools]
    print(json.dumps({
        "mcp_connected": True,
        "tool_count": len(names),
        "tool_names": names,
        "session_id_received": bool(session_id),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
