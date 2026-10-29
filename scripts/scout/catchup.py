# -*- coding: utf-8 -*-
"""兼容入口：历史回补已迁至 autotrader.scout.Scout.catchup（V4 切换后删除本文件）。"""
from __future__ import annotations

import argparse

from autotrader.scout import Scout


def main() -> None:
    parser = argparse.ArgumentParser(description="Scout 历史回补")
    parser.add_argument("--start", required=True, help="起始日 YYYYMMDD（含）")
    parser.add_argument("--end", required=True, help="结束日 YYYYMMDD（含）")
    parser.add_argument("--check", action="store_true", help="只检查缺口，不抓取")
    parser.add_argument("--replay", action="store_true", help="补完后链式重建快照")
    args = parser.parse_args()
    Scout().catchup(args.start, args.end, check=args.check, replay=args.replay)


if __name__ == "__main__":
    main()
