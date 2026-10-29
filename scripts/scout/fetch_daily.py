# -*- coding: utf-8 -*-
"""兼容入口：Scout 取数层已迁至 autotrader.scout.Scout.fetch_daily（V4 切换后删除本文件）。"""
from __future__ import annotations

import argparse

from autotrader.scout import Scout


def main() -> None:
    parser = argparse.ArgumentParser(description="Scout 取数层")
    parser.add_argument("--dates", nargs="+", required=True, help="交易日 YYYYMMDD，可多个")
    parser.add_argument("--light", action="store_true", help="轻量模式：仅日线+涨跌停价")
    parser.add_argument("--force", action="store_true",
                        help="已落盘的数据也重新拉取（占位，见 Scout.fetch_daily）")
    args = parser.parse_args()
    Scout().fetch_daily(args.dates, light=args.light, force=args.force)


if __name__ == "__main__":
    main()
