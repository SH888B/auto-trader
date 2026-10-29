# -*- coding: utf-8 -*-
"""Tushare 转接连通性探测：不走 tushare 包，直接按 tushare HTTP 协议 POST 到代理端点。
用法: python scripts/tushare_relay_probe.py
只读 config/tushare_relay.yaml；结果打印行数与字段，不回显 token。
"""
import json, re, sys, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
cfg = (ROOT / "config" / "tushare_relay.yaml").read_text(encoding="utf-8")
TOKEN = re.search(r"^token:\s*(\S+)", cfg, re.M).group(1)
URLS = re.findall(r"^url_\w+:\s*(\S+)", cfg, re.M)

def call(base, api, params):
    body = json.dumps({"api_name": api, "token": TOKEN, "params": params, "fields": ""}).encode()
    req = urllib.request.Request(base + "?api_name=" + api, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

PROBE = [
    ("trade_cal", {"exchange": "SSE", "start_date": "20261019", "end_date": "20261027"}),
    ("daily", {"trade_date": "20261023"}),
    ("stk_limit", {"trade_date": "20261023"}),
    ("stock_basic", {"list_status": "L"}),
    ("index_daily", {"ts_code": "000001.SH", "start_date": "20261020", "end_date": "20261023"}),
]

base = None
for u in URLS:
    try:
        d = call(u, "trade_cal", {"exchange": "SSE", "start_date": "20261026", "end_date": "20261026"})
        if d.get("code") == 0 or d.get("data"):
            base = u; print(f"[OK] 端点可用: {u}\n"); break
        print(f"[FAIL] {u}: {d.get('msg', d)[:100]}")
    except Exception as e:
        print(f"[ERROR] {u}: {e}")
if not base:
    sys.exit("所有端点均不可用")

for api, params in PROBE:
    try:
        d = call(base, api, params)
        items = (d.get("data") or {}).get("items", [])
        fields = (d.get("data") or {}).get("fields", [])
        print(f"{api:12s} params={params} -> {len(items)} 行; 字段: {fields[:12]}")
        if api == "trade_cal" and items:
            print("  交易日:", [i[1] for i in items if i[2] == 1] or items)
        if api == "daily" and items:
            print("  样例:", dict(zip(fields, items[0])))
    except Exception as e:
        print(f"{api}: ERROR {e}")
