# -*- coding: utf-8 -*-
"""auto-trader 命令入口。

用法：auto-trader <模块> <命令> [参数]，或 python -m autotrader <模块> <命令>。
各子命令随迁移进度逐步注册（见 DESIGN-V4 §六）。
"""
from __future__ import annotations

import argparse

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="auto-trader",
        description="首分机器人核心程序：数据 → 复盘 → 交易 → 执行 → 评价",
    )
    parser.add_argument("--version", action="version", version=f"auto-trader {__version__}")
    sub = parser.add_subparsers(dest="module")

    # 骨架阶段先占位：scout / curator / daily 等随迁移注册
    for name, help_text in [
        ("scout", "数据层：取数、加工、数仓、筛选、快照"),
        ("curator", "上下文：Digest 渲染、受控查询、Bundle、watchlist"),
        ("daily", "日常链路：prepare / continue / settle"),
        ("validate", "契约校验：review / intents"),
        ("score", "机械评分"),
        ("logs", "刷新日志视图"),
    ]:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("args", nargs="*", help="子命令参数（迁移完成后定义）")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.module:
        parser.print_help()
        return 0
    print(f"[auto-trader {__version__}] 模块 `{args.module}` 尚未迁移完成，暂不可用。"
          f"参数：{args.args}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
