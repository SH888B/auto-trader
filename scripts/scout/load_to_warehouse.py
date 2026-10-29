# -*- coding: utf-8 -*-
"""兼容入口：Scout 数仓入库已迁至 autotrader.scout.Scout.load_warehouse（V4 切换后删除本文件）。"""
from __future__ import annotations

import argparse

from autotrader.scout import Scout


def main() -> None:
    parser = argparse.ArgumentParser(description="Scout 数仓入库")
    parser.add_argument("--date", help="单日 YYYYMMDD")
    parser.add_argument("--all", action="store_true", help="全量（扫描 raw 全部日期）")
    args = parser.parse_args()
    Scout().load_warehouse(date=args.date, all_dates=args.all)


if __name__ == "__main__":
    main()
