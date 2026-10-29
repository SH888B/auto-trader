# -*- coding: utf-8 -*-
"""兼容入口：Curator 已迁至 autotrader.curator.Curator.build_bundle（V4 切换后删除本文件）。"""
from __future__ import annotations

import argparse

from autotrader.curator import Curator


def main() -> None:
    parser = argparse.ArgumentParser(description="Curator：为实验打包 bundle3.0 数据包")
    parser.add_argument("--experiment", required=True, help="实验 ID，如 E008")
    args = parser.parse_args()
    Curator().build_bundle(args.experiment)


if __name__ == "__main__":
    main()
