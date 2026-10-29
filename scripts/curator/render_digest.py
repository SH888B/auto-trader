# -*- coding: utf-8 -*-
"""兼容入口：Curator 逻辑已迁至 autotrader.curator.Curator.render_digest（V4 切换后删除）。"""
from __future__ import annotations

import argparse

from autotrader.curator import Curator


def main() -> None:
    parser = argparse.ArgumentParser(description="Curator：按 View Recipe 渲染确定性 Digest")
    parser.add_argument("--date", required=True, help="决策时点 YYYYMMDD")
    parser.add_argument("--view", default="configs/digest/judge_default.yaml",
                        help="View Recipe 路径")
    parser.add_argument("--run-id", help="Run ID；默认 live-<date>")
    parser.add_argument("--module", default="judge", choices=["judge", "trader"], help="目标模块")
    parser.add_argument("--output", help="输出路径；默认 runs/live/<date>/<module>/digest.md")
    args = parser.parse_args()
    Curator().render_digest(
        args.date, view=args.view, run_id=args.run_id, module=args.module, output=args.output,
    )


if __name__ == "__main__":
    main()
