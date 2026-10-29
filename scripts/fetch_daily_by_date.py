# -*- coding: utf-8 -*-
"""按交易日批量拉取全市场日线，存 CSV。用于 JDFS 会议数据回译。
Usage: python scripts/fetch_daily_by_date.py 20260514 20260515 ...
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "raw"
OUT.mkdir(parents=True, exist_ok=True)
TOKEN = os.environ["TUSHARE_TOKEN"]
URL = "https://api.tushare.pro/mcp/?token=" + TOKEN
FIELDS = ["ts_code", "trade_date", "open", "high", "low", "close", "pre_close",
          "change", "pct_chg", "vol", "amount"]


def call(tool: str, params: dict):
    r = requests.post(URL, headers={"Content-Type": "application/json",
                                    "Accept": "application/json, text/event-stream"},
                      json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": tool, "arguments": params}}, timeout=120)
    r.raise_for_status()
    text = r.content.decode("utf-8")
    if "text/event-stream" in r.headers.get("content-type", ""):
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        data = []
        for ev in text.split("\n\n"):
            payload = "\n".join(l[5:].lstrip() for l in ev.splitlines() if l.startswith("data:")).strip()
            if payload and payload != "[DONE]":
                data.append(json.loads(payload))
        text = data[0]["result"]["content"][0]["text"]
    return json.loads(text)


def main():
    dates = sys.argv[1:]
    for d in dates:
        out = OUT / f"daily_{d}.csv"
        if out.exists():
            print(f"skip {d}")
            continue
        for attempt in range(3):
            try:
                rows = call("daily", {"trade_date": d, "fields": FIELDS})
                if not rows:
                    print(f"{d}: EMPTY (非交易日?)")
                    break
                import csv
                with open(out, "w", newline="", encoding="utf-8-sig") as f:
                    w = csv.DictWriter(f, fieldnames=FIELDS)
                    w.writeheader()
                    w.writerows(rows)
                print(f"{d}: {len(rows)} rows")
                break
            except Exception as e:  # noqa
                print(f"{d}: attempt{attempt} error {e}")
                time.sleep(3)
        time.sleep(0.5)


if __name__ == "__main__":
    main()
