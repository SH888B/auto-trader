# -*- coding: utf-8 -*-
"""实验模式：独立 state 视图 + 幂等推进器，回放任意日期区间的完整链路。

与 live（Daily）的差异只有三点：
  1. 产物落 ``experiments/<id>/runs/<date>/``（run_root 注入）
  2. watchlist/portfolio 用实验私有 state，collect 不污染 live（构造函数注入）
  3. ``run(start, end)`` 按区间内实际有快照的交易日自动推进：
     数据与任务包可全自动；遇到缺 LLM 产物的日子打印待办并继续下一日。

用法（CLI 见 ``auto-trader experiment --help``）::

    Experiment("E008").setup()                      # 建目录 + 复制基线 state
    Experiment("E008").run("20261019", "20261028")  # 幂等推进
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .core import RunLogger, Workspace, WorkspaceError
from .pipeline.daily import Daily

DEFAULT_WATCHLIST_BASE = "state/watchlist/judge/current.yaml"
DEFAULT_USER_WATCHLIST_BASE = "state/watchlist/user/current.yaml"
DEFAULT_PORTFOLIO_BASE = "state/portfolio/current.yaml"


class Experiment:
    """一个实验 = 独立 runs 目录 + 独立 watchlist/portfolio 状态。"""

    def __init__(self, experiment_id: str, workspace: Workspace | None = None,
                 logger: RunLogger | None = None, exp_root: str = "experiments",
                 watchlist_base: str | None = None,
                 user_watchlist_base: str | None = None,
                 portfolio_base: str | None = None,
                 judge_profile: str | None = None,
                 trader_profile: str | None = None) -> None:
        if not experiment_id or any(c in experiment_id for c in "\\/:*?\"<>|"):
            raise WorkspaceError(f"实验 ID 非法：{experiment_id!r}")
        self.ws = workspace or Workspace()
        self.logger = logger or RunLogger()
        self.experiment_id = experiment_id
        self.exp_root = exp_root
        self.root_rel = f"{exp_root}/{experiment_id}"
        self.watchlist_base = watchlist_base or DEFAULT_WATCHLIST_BASE
        self.user_watchlist_base = user_watchlist_base or DEFAULT_USER_WATCHLIST_BASE
        self.portfolio_base = portfolio_base or DEFAULT_PORTFOLIO_BASE
        self.judge_profile = judge_profile
        self.trader_profile = trader_profile

    # ------------------------------------------------------------------ #
    # 路径约定
    # ------------------------------------------------------------------ #

    @property
    def root(self) -> Path:
        return self.ws.path(self.root_rel)

    @property
    def run_root(self) -> str:
        return f"{self.root_rel}/runs"

    @property
    def state_root(self) -> str:
        """实验私有 state 根（judge/user watchlist、portfolio 都在这下面）。"""
        return f"{self.root_rel}/state"

    @property
    def watchlist_target(self) -> str:
        return f"{self.state_root}/watchlist/judge/current.yaml"

    @property
    def user_watchlist_target(self) -> str:
        return f"{self.state_root}/watchlist/user/current.yaml"

    @property
    def portfolio_path(self) -> str:
        return f"{self.state_root}/portfolio/current.yaml"

    def _daily(self) -> Daily:
        return Daily(self.ws, self.logger, run_root=self.run_root,
                     judge_profile=self.judge_profile,
                     trader_profile=self.trader_profile,
                     state_root=self.state_root,
                     watchlist_target=self.watchlist_target,
                     portfolio_path=self.portfolio_path)

    # ------------------------------------------------------------------ #
    # setup：建目录结构 + 复制基线 state（防覆盖已有实验）
    # ------------------------------------------------------------------ #

    def setup(self) -> Path:
        """创建实验目录并复制基线 watchlist/portfolio。已存在则拒绝。"""
        if self.root.exists() and any(self.root.iterdir()):
            raise WorkspaceError(
                f"实验目录已存在且非空，拒绝覆盖：{self.root_rel}")
        for rel in (f"{self.root_rel}/runs",
                    f"{self.root_rel}/state/watchlist/judge/history",
                    f"{self.root_rel}/state/watchlist/user/history",
                    f"{self.root_rel}/state/portfolio/history"):
            self.ws.path(rel).mkdir(parents=True, exist_ok=True)
        self._copy_base(self.watchlist_base, self.watchlist_target)
        self._ensure_user_watchlist()
        self._copy_base(self.portfolio_base, self.portfolio_path)
        print(f"[experiment] {self.experiment_id} 就绪：{self.root_rel}"
              f"（watchlist 基线 {self.watchlist_base}）")
        return self.root

    def _copy_base(self, base_rel: str, target_rel: str) -> None:
        base = self.ws.path(base_rel)
        if not base.is_file():
            raise WorkspaceError(f"基线 state 缺失：{base_rel}")
        target = self.ws.path(target_rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(base, target)

    def _copy_history_from(self, base_rel: str, target_rel: str,
                           before: str | None = None) -> None:
        """把基线 state 的历史快照复制进实验（只复制 ``< before`` 的日期）。"""
        base = self.ws.path(Path(base_rel).parent / "history")
        if not base.is_dir():
            return
        target = self.ws.path(target_rel)
        target.mkdir(parents=True, exist_ok=True)
        for source in sorted(base.glob("*.yaml")):
            if before and not source.stem < before:
                continue
            destination = target / source.name
            if not destination.exists():
                shutil.copy2(source, destination)

    def _ensure_history(self, as_of: str) -> None:
        """实验推进某日前，补齐该日之前的历史快照（绝不复制当日）。"""
        day = as_of if "-" in as_of else f"{as_of[:4]}-{as_of[4:6]}-{as_of[6:]}"
        self._copy_history_from(self.watchlist_base,
                                f"{self.root_rel}/state/watchlist/judge/history",
                                before=day)
        self._copy_history_from(self.user_watchlist_base,
                                f"{self.root_rel}/state/watchlist/user/history",
                                before=day)

    def _ensure_user_watchlist(self) -> None:
        """实验私有 user watchlist：有基线就复制，没有就建空模板（用户后续维护）。"""
        target = self.ws.path(self.user_watchlist_target)
        if self.ws.path(self.user_watchlist_base).is_file():
            self._copy_base(self.user_watchlist_base, self.user_watchlist_target)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text("version: u0.0\nstocks: []\n", encoding="utf-8")

    # ------------------------------------------------------------------ #
    # 单日：委托 Daily（注入实验 run_root 与 state）
    # ------------------------------------------------------------------ #

    def prepare(self, as_of: str) -> None:
        """数据 → Judge 任务包（实验目录）。幂等。"""
        self._ensure_history(as_of)
        self._daily().prepare(as_of)

    def continue_(self, as_of: str) -> None:
        """验收 review → Trader 任务包（实验目录）。幂等。"""
        self._ensure_history(as_of)
        self._daily().continue_(as_of)

    def collect_trader(self, as_of: str) -> dict:
        """验收 order_intents.json（实验目录）。"""
        return self._daily().trader.collect(as_of)

    # ------------------------------------------------------------------ #
    # run：区间内自动推进（幂等，可反复执行直到全部完成）
    # ------------------------------------------------------------------ #

    def run(self, start: str, end: str, *, with_data: bool = False) -> None:
        """对区间内每个有快照的交易日推进链路，能自动的自动、不能的列待办。

        Args:
            start/end: YYYYMMDD（含）。
            with_data: 缺快照时自动跑 Scout 全日链（历史日外部数据源可能缺失，
                默认 False——建议先用 ``scout catchup --replay`` 备数）。
        """
        days = self._snapshot_days(start, end)
        if not days:
            print(f"[experiment] 区间内无快照：{start}~{end}"
                  f"（先跑 scout catchup --start {start} --end {end} --replay）")
            return
        print(f"[experiment] {self.experiment_id} 推进 {days[0]} ~ {days[-1]}，"
              f"共 {len(days)} 个交易日")
        for date_dash in days:
            self._run_one(date_dash, with_data=with_data)

    def _snapshot_days(self, start: str, end: str) -> list[str]:
        start_d, end_d = self._dash(start), self._dash(end)
        snap_root = self.ws.path("data/snapshots")
        if not snap_root.is_dir():
            return []
        return sorted(d.name for d in snap_root.iterdir()
                      if d.is_dir() and start_d <= d.name <= end_d
                      and (d / "snapshot.yaml").is_file())

    def _run_one(self, date_dash: str, *, with_data: bool) -> None:
        snapshot = self.ws.path(f"data/snapshots/{date_dash}/snapshot.yaml")
        if not snapshot.is_file():
            if with_data:
                self._daily().scout.run_date(date_dash.replace("-", ""))
            else:
                print(f"[{date_dash}] 缺快照，跳过（或加 --with-data 自动补跑）")
                return
        todo: list[str] = []
        try:
            self.prepare(date_dash)
            review = self.ws.path(
                f"{self.run_root}/{date_dash}/judge/review.json")
            if review.is_file():
                self.continue_(date_dash)
                intents = self.ws.path(
                    f"{self.run_root}/{date_dash}/arms/manual/trader/"
                    "order_intents.json")
                if intents.is_file():
                    self.collect_trader(date_dash)
                else:
                    todo.append("Trader 任务包待执行（落盘 order_intents.json 后重跑）")
            else:
                todo.append("Judge 任务包待执行（落盘 review.json 后重跑）")
        except WorkspaceError as exc:
            todo.append(f"链路错误：{exc}")
        status = "；待办：" + "；".join(todo) if todo else "完成"
        print(f"[{date_dash}] {status}")

    @staticmethod
    def _dash(date: str) -> str:
        return f"{date[:4]}-{date[4:6]}-{date[6:]}" if "-" not in date else date


__all__ = ["Experiment"]
