# -*- coding: utf-8 -*-
"""兼容入口：板块成分取数已迁至 autotrader.scout.Scout.fetch_theme_members。

V4 切换后删除本文件。
"""
from __future__ import annotations

import argparse

from autotrader.scout import Scout


def main() -> None:
    parser = argparse.ArgumentParser(description="Scout：板块成分取数入库（按快照日）")
    parser.add_argument("--date",
                        help="快照戳 YYYYMMDD（= 本成分要服务的决策日）；默认最近板块榜日期")
    parser.add_argument("--boards", help="显式板块代码清单（逗号分隔），覆盖 --date 的板块清单")
    parser.add_argument("--max-age-days", type=float, default=7, help="同戳 raw 缓存新鲜期（天）")
    parser.add_argument("--force", action="store_true", help="忽略缓存，重取并覆盖该戳快照")
    parser.add_argument("--ingest-all", action="store_true", help="不取数，仅把 raw 全量灌入数仓")
    args = parser.parse_args()
    boards = [b.strip() for b in args.boards.split(",") if b.strip()] if args.boards else None
    Scout().fetch_theme_members(args.date, boards=boards, max_age_days=args.max_age_days,
                                force=args.force, ingest_all=args.ingest_all)


if __name__ == "__main__":
    main()
