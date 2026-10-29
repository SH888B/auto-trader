# -*- coding: utf-8 -*-
"""auto-trader 命令入口。

用法：auto-trader <模块> <命令> [参数]，或 python -m autotrader <模块> <命令>。
当前已注册：scout（数据层）、curator（上下文层）；
daily / validate / score / logs 随迁移进度注册（见 DESIGN-V4 §六）。
"""
from __future__ import annotations

import argparse
from typing import Any

from . import __version__

# --------------------------------------------------------------------------- #
# 子命令 handler（薄分发，逻辑全在 Scout / Curator 类内）
# --------------------------------------------------------------------------- #

def _scout_run(args: argparse.Namespace) -> None:
    from .scout import Scout

    Scout().run_date(args.date, auto_fetch=not args.no_auto_fetch)


def _scout_fetch(args: argparse.Namespace) -> int:
    from .fetch import FetchIncomplete
    from .scout import Scout

    # --force 为兼容旧命令保留，现在表示强制重抓
    try:
        Scout().fetch_daily(args.dates, light=args.light, force=args.force)
    except FetchIncomplete as exc:
        print(f"[blocked] {exc}")
        return 4
    return 0


def _scout_load(args: argparse.Namespace) -> None:
    from .scout import Scout

    if args.ingest_all:
        Scout().load_warehouse(all_dates=True)
    else:
        Scout().load_warehouse(date=args.date, all_dates=args.all)


def _scout_build(args: argparse.Namespace) -> None:
    from .scout import Scout

    Scout().build_snapshot(args.date, auto_fetch=not args.no_auto_fetch)


def _scout_select(args: argparse.Namespace) -> None:
    from .scout import Scout

    Scout().select_stocks(args.date)


def _scout_finalize(args: argparse.Namespace) -> None:
    from .scout import Scout

    Scout().finalize(args.date)


def _scout_catchup(args: argparse.Namespace) -> None:
    from .scout import Scout

    Scout().catchup(args.start, args.end, check=args.check, replay=args.replay)


def _scout_members(args: argparse.Namespace) -> None:
    from .scout import Scout

    boards = [b.strip() for b in args.boards.split(",")] if args.boards else None
    Scout().fetch_theme_members(args.date, boards=boards,
                                max_age_days=args.max_age_days)


def _scout_sync(args: argparse.Namespace) -> int:
    from .fetch import Fetcher

    result = Fetcher().sync(args.date, window=args.window,
                            deadline_minutes=args.deadline, force=args.force)
    for name, record in result["results"].items():
        print(f"  {name:22s} {str(record.get('status')):16s} rows={record.get('rows', 0)}")
    readiness = result["readiness"]
    print(f"[readiness] {readiness['status']} "
          f"blocking={readiness['blocking'] or '-'} "
          f"degraded_by={readiness['degraded_by'] or '-'}")
    if readiness["status"] == "blocked":
        return 4
    if readiness["status"] == "degraded":
        return 3
    return 0


def _scout_status(args: argparse.Namespace) -> int:
    import json

    from .fetch import Fetcher

    readiness = Fetcher().status(args.date)
    if args.json:
        print(json.dumps(readiness, ensure_ascii=False, indent=2))
    else:
        print(f"{readiness['trade_date']}  status={readiness['status']}")
        for name, detail in readiness["detail"].items():
            print(f"  {name:22s} {str(detail.get('state')):16s} "
                  f"rows={detail.get('rows')} "
                  f"rev={detail.get('revision', '-')} "
                  f"avail={detail.get('available_at', '-')}")
        if readiness["gaps"]:
            print(f"  gaps: {readiness['gaps']}")
    if readiness["status"] == "blocked":
        return 4
    if readiness["status"] == "degraded":
        return 3
    return 0


def _scout_verify(args: argparse.Namespace) -> int:
    from .fetch import Fetcher

    issues = Fetcher().verify(args.date)
    if not issues:
        print(f"[OK] {args.date} 账本与 raw 一致")
        return 0
    for issue in issues:
        print(f"[问题] {issue}")
    return 1


def _curator_digest(args: argparse.Namespace) -> None:
    from .curator import Curator

    Curator().render_digest(args.date, view=args.view, run_id=args.run_id,
                            module=args.module, output=args.output)


def _curator_query(args: argparse.Namespace) -> None:
    from .core import load_yaml
    from .curator import Curator

    Curator().query(load_yaml(args.request), output_dir=args.output_dir)


def _curator_watchlist_apply(args: argparse.Namespace) -> None:
    from .curator import Curator

    Curator().apply_watchlist_updates(args.review, args.target,
                                      snapshot=args.snapshot)


def _daily_prepare(args: argparse.Namespace) -> int:
    from .core import WorkspaceError
    from .pipeline.daily import Daily

    try:
        Daily().prepare(args.as_of, allow_degraded=args.allow_degraded)
    except WorkspaceError as exc:
        print(f"[blocked] {exc}")
        return 4
    return 0


def _daily_continue(args: argparse.Namespace) -> None:
    from .pipeline.daily import Daily

    Daily().continue_(args.as_of)


def _validate_review(args: argparse.Namespace) -> int:
    import json

    from .core import Workspace
    from .pipeline.validate import validate_review

    result = validate_review(json.loads(Workspace().path(args.path)
                                       .read_text(encoding="utf-8")))
    for w in result.warnings:
        print(f"[警告] {w}")
    if result.errors:
        print(f"[校验失败] {args.path}")
        for e in result.errors:
            print(f"  - {e}")
        return 1
    print(f"[OK] {args.path} 符合 review 语义校验")
    return 0


def _validate_intents(args: argparse.Namespace) -> int:
    import json

    from .core import Workspace
    from .pipeline.validate import validate_order_intents

    data = json.loads(Workspace().path(args.path).read_text(encoding="utf-8"))
    result = validate_order_intents(data)
    for w in result.warnings:
        print(f"[警告] {w}")
    if result.errors:
        print(f"[校验失败] {args.path}")
        for e in result.errors:
            print(f"  - {e}")
        return 1
    print(f"[OK] {args.path}（{len(data.get('actions', []))} 个订单意图）")
    return 0


def _experiment_setup(args: argparse.Namespace) -> None:
    from .experiment import Experiment

    Experiment(args.experiment,
               watchlist_base=args.watchlist_base,
               portfolio_base=args.portfolio_base).setup()


def _experiment_prepare(args: argparse.Namespace) -> None:
    from .experiment import Experiment

    Experiment(args.experiment).prepare(args.date)


def _experiment_continue(args: argparse.Namespace) -> None:
    from .experiment import Experiment

    Experiment(args.experiment).continue_(args.date)


def _experiment_run(args: argparse.Namespace) -> None:
    from .experiment import Experiment

    Experiment(args.experiment).run(args.start, args.end,
                                    with_data=args.with_data)


# --------------------------------------------------------------------------- #
# 占位模块（第 8 步注册：score / logs）
# --------------------------------------------------------------------------- #

def _not_yet(module: str) -> Any:
    def handler(args: argparse.Namespace) -> int:
        print(f"[auto-trader {__version__}] 模块 `{module}` 尚未迁移完成，暂不可用。")
        return 2

    return handler


# --------------------------------------------------------------------------- #
# 解析器组装
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="auto-trader",
        description="首分机器人核心程序：数据 → 复盘 → 交易 → 执行 → 评价",
    )
    parser.add_argument("--version", action="version", version=f"auto-trader {__version__}")
    sub = parser.add_subparsers(dest="module")

    # ------------------------------ scout ------------------------------ #
    p_scout = sub.add_parser("scout", help="数据层：取数、加工、数仓、筛选、快照")
    ssub = p_scout.add_subparsers(dest="command", required=True)

    p = ssub.add_parser("run", help="单日完整数据链：取数→入库→筛选→冻结")
    p.add_argument("--date", required=True, help="交易日 YYYYMMDD")
    p.add_argument("--no-auto-fetch", action="store_true",
                   help="历史窗口缺失时不自动补拉")
    p.set_defaults(handler=_scout_run)

    p = ssub.add_parser("fetch", help="按日抓取行情原始数据")
    p.add_argument("--dates", nargs="+", required=True, help="交易日 YYYYMMDD，可多个")
    p.add_argument("--light", action="store_true", help="轻量模式：仅日线+涨跌停价")
    p.add_argument("--force", action="store_true", help="保留参数，当前不生效")
    p.set_defaults(handler=_scout_fetch)

    p = ssub.add_parser("load", help="把 raw 灌入 DuckDB 数仓")
    p.add_argument("--date", help="单日 YYYYMMDD")
    p.add_argument("--all", action="store_true", help="全量（扫描 raw 全部日期）")
    p.add_argument("--ingest-all", action="store_true", help="不取数，仅把 raw 全量灌入数仓")
    p.set_defaults(handler=_scout_load)

    p = ssub.add_parser("build", help="加工层：生成 curated 数据与核对报告")
    p.add_argument("--date", required=True, help="交易日 YYYYMMDD")
    p.add_argument("--no-auto-fetch", action="store_true",
                   help="历史窗口缺失时不自动补拉")
    p.set_defaults(handler=_scout_build)

    p = ssub.add_parser("select", help="按 selectors 配方筛选标的")
    p.add_argument("--date", required=True, help="交易日 YYYYMMDD")
    p.set_defaults(handler=_scout_select)

    p = ssub.add_parser("finalize", help="冻结某日快照清单 snapshot.yaml")
    p.add_argument("--date", required=True, help="交易日 YYYYMMDD")
    p.set_defaults(handler=_scout_finalize)

    p = ssub.add_parser("catchup", help="历史回补：核对缺口→补拉→可选链式重建")
    p.add_argument("--start", required=True, help="起始日 YYYYMMDD（含）")
    p.add_argument("--end", required=True, help="结束日 YYYYMMDD（含）")
    p.add_argument("--check", action="store_true", help="只检查缺口，不抓取")
    p.add_argument("--replay", action="store_true", help="补完后链式重建快照")
    p.set_defaults(handler=_scout_catchup)

    p = ssub.add_parser("members", help="抓取题材板块成分股")
    p.add_argument("--date", required=True, help="交易日 YYYYMMDD")
    p.add_argument("--boards", help="显式板块代码清单（逗号分隔），覆盖 --date 的清单")
    p.add_argument("--max-age-days", type=float, default=7,
                   help="同戳 raw 缓存新鲜期（天）")
    p.set_defaults(handler=_scout_members)

    p = ssub.add_parser("sync", help="按数据源契约抓取一个窗口并记账（幂等）")
    p.add_argument("--date", required=True, help="交易日 YYYYMMDD 或 YYYY-MM-DD")
    p.add_argument("--window", default="all", choices=["all", "evening", "morning"],
                   help="调度窗口：evening=收盘后，morning=次日早间增强")
    p.add_argument("--deadline", type=float, default=30.0,
                   help="本次同步的总时限（分钟），到点即停并保留 pending")
    p.add_argument("--force", action="store_true",
                   help="强制重抓（绕过已 ok 跳过与 not_due）")
    p.set_defaults(handler=_scout_sync)

    p = ssub.add_parser("status", help="查看某日数据就绪度（readiness）")
    p.add_argument("--date", required=True, help="交易日 YYYYMMDD 或 YYYY-MM-DD")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.set_defaults(handler=_scout_status)

    p = ssub.add_parser("verify", help="账本与 raw 对账（sha256/行数/缺失）")
    p.add_argument("--date", required=True, help="交易日 YYYYMMDD 或 YYYY-MM-DD")
    p.set_defaults(handler=_scout_verify)

    # ------------------------------ curator ------------------------------ #
    p_curator = sub.add_parser("curator", help="上下文：Digest 渲染、受控查询、Bundle、watchlist")
    csub = p_curator.add_subparsers(dest="command", required=True)

    p = csub.add_parser("digest", help="按 View Recipe 渲染确定性 Digest")
    p.add_argument("--date", required=True, help="决策时点 YYYYMMDD")
    p.add_argument("--view", default="configs/digest/judge_default.yaml",
                   help="View Recipe 路径")
    p.add_argument("--run-id", help="Run ID；默认 live-<date>")
    p.add_argument("--module", default="judge", choices=["judge", "trader"],
                   help="目标模块")
    p.add_argument("--output", help="输出路径；默认 runs/live/<date>/<module>/digest.md")
    p.set_defaults(handler=_curator_digest)

    p = csub.add_parser("query", help="按查询请求 YAML 拉取点-in-time 数据")
    p.add_argument("--request", required=True, help="查询请求 YAML")
    p.add_argument("--output-dir", help="查询产物目录；默认使用请求中的 output_dir 或 live Run")
    p.set_defaults(handler=_curator_query)

    p = csub.add_parser("watchlist", help="Judge watchlist 维护")
    wsub = p.add_subparsers(dest="command", required=True)
    p = wsub.add_parser("apply", help="应用 Judge review 的 watchlist 更新并冻结快照")
    p.add_argument("--review", required=True, help="Judge review JSON 路径")
    p.add_argument("--target", required=True, help="Judge watchlist YAML，不允许 user watchlist")
    p.add_argument("--snapshot", help="历史快照路径；默认 target 同级 history/<as_of>.yaml")
    p.set_defaults(handler=_curator_watchlist_apply)

    # ------------------------------ daily ------------------------------ #
    p_daily = sub.add_parser("daily", help="日常链路：prepare / continue / settle")
    dsub = p_daily.add_subparsers(dest="command", required=True)
    p = dsub.add_parser("prepare", help="数据 → Judge 交接包（幂等，可重跑）")
    p.add_argument("--as-of", required=True, help="交易日 YYYY-MM-DD 或 YYYYMMDD")
    p.add_argument("--allow-degraded", action="store_true",
                   help="允许在 degraded 数据上继续（缺口会写入 input_manifest）")
    p.set_defaults(handler=_daily_prepare)
    p = dsub.add_parser("continue", help="验收 review → Trader 交接包（幂等，可重跑）")
    p.add_argument("--as-of", required=True, help="交易日 YYYY-MM-DD 或 YYYYMMDD")
    p.set_defaults(handler=_daily_continue)

    # ------------------------------ validate ------------------------------ #
    p_validate = sub.add_parser("validate", help="契约校验：review / intents")
    vsub = p_validate.add_subparsers(dest="command", required=True)
    p = vsub.add_parser("review", help="Judge review schema + 语义校验")
    p.add_argument("path", help="review.json 路径（workspace 内）")
    p.set_defaults(handler=_validate_review)
    p = vsub.add_parser("intents", help="Trader order_intents schema + 引用校验")
    p.add_argument("path", help="order_intents.json 路径（workspace 内）")
    p.set_defaults(handler=_validate_intents)

    # ------------------------------ experiment ------------------------------ #
    p_exp = sub.add_parser("experiment", help="实验回放：独立 state + 幂等推进")
    esub = p_exp.add_subparsers(dest="command", required=True)
    p = esub.add_parser("setup", help="建实验目录并复制基线 state（防覆盖）")
    p.add_argument("experiment", help="实验 ID，如 E008")
    p.add_argument("--watchlist-base", help="judge watchlist 基线文件；默认 live current")
    p.add_argument("--portfolio-base", help="portfolio 基线文件；默认 live current")
    p.set_defaults(handler=_experiment_setup)
    p = esub.add_parser("prepare", help="实验某日：数据 → Judge 任务包")
    p.add_argument("experiment", help="实验 ID")
    p.add_argument("--date", required=True, help="交易日 YYYYMMDD 或 YYYY-MM-DD")
    p.set_defaults(handler=_experiment_prepare)
    p = esub.add_parser("continue", help="实验某日：验收 review → Trader 任务包")
    p.add_argument("experiment", help="实验 ID")
    p.add_argument("--date", required=True, help="交易日 YYYYMMDD 或 YYYY-MM-DD")
    p.set_defaults(handler=_experiment_continue)
    p = esub.add_parser("run", help="幂等推进区间：自动的自动，缺 LLM 产物的列待办")
    p.add_argument("experiment", help="实验 ID")
    p.add_argument("--start", required=True, help="起始日 YYYYMMDD（含）")
    p.add_argument("--end", required=True, help="结束日 YYYYMMDD（含）")
    p.add_argument("--with-data", action="store_true",
                   help="缺快照时自动跑 Scout 全日链（历史外部数据可能缺失）")
    p.set_defaults(handler=_experiment_run)

    # ------------------------------ 占位 ------------------------------ #
    for name, help_text in [
        ("score", "机械评分"),
        ("logs", "刷新日志视图"),
    ]:
        p = sub.add_parser(name, help=f"（待迁移）{help_text}")
        p.add_argument("args", nargs="*", help="子命令参数（迁移完成后定义）")
        p.set_defaults(handler=_not_yet(name))

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.module:
        parser.print_help()
        return 0
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    result = handler(args)
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
