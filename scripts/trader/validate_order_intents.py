# -*- coding: utf-8 -*-
"""兼容入口：校验逻辑已迁至 autotrader.pipeline.validate（V4 切换后删除本文件）。"""
from __future__ import annotations

import argparse
import json
import sys

from autotrader.core import Workspace
from autotrader.pipeline.validate import validate_order_intents


def main() -> int:
    parser = argparse.ArgumentParser(description="Trader order_intents 校验")
    parser.add_argument("instance", help="order_intents.json 路径（workspace 内）")
    args = parser.parse_args()

    data = json.loads(Workspace().path(args.instance).read_text(encoding="utf-8"))
    result = validate_order_intents(data)
    for w in result.warnings:
        print(f"[警告] {w}")
    if result.errors:
        print("[校验失败]")
        for e in result.errors:
            print(f"  - {e}")
        return 1
    print(f"[OK] {args.instance}（{len(data.get('actions', []))} 个订单意图）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
