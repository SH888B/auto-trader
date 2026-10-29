# -*- coding: utf-8 -*-
"""兼容入口：Scout 筛选层已迁至 autotrader.scout.Scout.select_stocks（V4 切换后删除本文件）。"""
from __future__ import annotations

import argparse

from autotrader.scout import Scout


def main() -> None:
    parser = argparse.ArgumentParser(description="Scout 筛选层")
    parser.add_argument("--date", required=True, help="交易日 YYYYMMDD")
    args = parser.parse_args()
    Scout().select_stocks(args.date)


if __name__ == "__main__":
    main()
