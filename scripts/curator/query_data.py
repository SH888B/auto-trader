# -*- coding: utf-8 -*-
"""兼容入口：Curator 已迁至 autotrader.curator.Curator.query（V4 切换后删除本文件）。"""
from __future__ import annotations

import argparse

from autotrader.core import load_yaml
from autotrader.curator import Curator


def main() -> None:
    parser = argparse.ArgumentParser(description="Curator：按查询请求 YAML 拉取点-in-time 数据")
    parser.add_argument("--request", required=True, help="查询请求 YAML")
    parser.add_argument("--output-dir",
                        help="查询产物目录；默认使用请求中的 output_dir 或 live Run")
    args = parser.parse_args()
    Curator().query(load_yaml(args.request), output_dir=args.output_dir)


if __name__ == "__main__":
    main()
