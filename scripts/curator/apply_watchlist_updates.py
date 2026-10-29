# -*- coding: utf-8 -*-
"""兼容入口：Curator 逻辑已迁至 autotrader.curator.Curator.apply_watchlist_updates（V4 删除）。"""
from __future__ import annotations

import argparse

from autotrader.curator import Curator


def main() -> None:
    parser = argparse.ArgumentParser(description="Curator：应用 Judge review 的 watchlist 更新")
    parser.add_argument("--review", required=True, help="Judge review JSON 路径")
    parser.add_argument("--target", required=True,
                        help="Judge watchlist YAML，不允许 user watchlist")
    parser.add_argument("--snapshot", help="历史快照路径；默认 target 同级 history/<as_of>.yaml")
    args = parser.parse_args()
    Curator().apply_watchlist_updates(args.review, args.target, snapshot=args.snapshot)


if __name__ == "__main__":
    main()
