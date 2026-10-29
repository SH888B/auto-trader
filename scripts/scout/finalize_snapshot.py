# -*- coding: utf-8 -*-
"""兼容入口：快照冻结已迁至 autotrader.scout.Scout.finalize（V4 切换后删除本文件）。"""
from __future__ import annotations

import argparse

from autotrader.scout import Scout


def main() -> None:
    parser = argparse.ArgumentParser(description="Scout：冻结数据快照清单（不生成 Digest）")
    parser.add_argument("--date", required=True, help="交易日 YYYYMMDD")
    args = parser.parse_args()
    Scout().finalize(args.date)


if __name__ == "__main__":
    main()
