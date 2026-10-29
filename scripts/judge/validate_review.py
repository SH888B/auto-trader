# -*- coding: utf-8 -*-
"""兼容入口：校验逻辑已迁至 autotrader.pipeline.validate（V4 切换后删除本文件）。"""
from __future__ import annotations

import argparse
import json
import sys

from autotrader.core import Workspace
from autotrader.pipeline.validate import validate_review


def main() -> int:
    parser = argparse.ArgumentParser(description="Judge review 语义校验")
    parser.add_argument("review", help="review.json 路径（相对 workspace 根或绝对路径）")
    args = parser.parse_args()

    review = json.loads(Workspace().path(args.review).read_text(encoding="utf-8"))
    result = validate_review(review)
    for w in result.warnings:
        print(f"[警告] {w}")
    if result.errors:
        print(f"[校验失败] {args.review}")
        for e in result.errors:
            print(f"  - {e}")
        return 1
    print(f"[OK] {args.review} 符合 review 语义校验")
    return 0


if __name__ == "__main__":
    sys.exit(main())
