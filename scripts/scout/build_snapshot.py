# -*- coding: utf-8 -*-
"""兼容入口：Scout 加工层已迁至 autotrader.scout.Scout.build_snapshot（V4 切换后删除本文件）。"""
from __future__ import annotations

import argparse

from autotrader.scout import Scout


def main() -> None:
    parser = argparse.ArgumentParser(description="Scout 加工层：生成 curated 数据与核对报告")
    parser.add_argument("--date", required=True, help="交易日 YYYYMMDD")
    parser.add_argument("--no-auto-fetch", action="store_true", help="历史窗口缺失时不自动补拉")
    args = parser.parse_args()
    Scout().build_snapshot(args.date, auto_fetch=not args.no_auto_fetch)


if __name__ == "__main__":
    main()
