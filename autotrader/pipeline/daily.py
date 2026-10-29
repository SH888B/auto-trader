# -*- coding: utf-8 -*-
"""日常链路编排：Daily.prepare / continue_ / settle。

每步失败：run_events.jsonl 写 error、停在原地；重跑同一命令从断点继续
（已存在的产物直接跳过）。设计稿 §二 class Daily。
"""
from __future__ import annotations

from ..core import RunLogger, Workspace
from ..curator import Curator
from ..judge import Judge
from ..scout import Scout
from ..trader import Trader

TRADER_VIEW = "configs/digest/trader_initial.yaml"


class Daily:
    """编排只有一个类，顺序调用 Scout/Curator/Judge/Trader。"""

    def __init__(self, workspace: Workspace | None = None,
                 logger: RunLogger | None = None,
                 run_root: str = "runs/live",
                 judge_profile: str | None = None,
                 trader_profile: str | None = None) -> None:
        self.ws = workspace or Workspace()
        self.logger = logger or RunLogger()
        self.run_root = run_root
        judge_kw = {"run_root": run_root}
        trader_kw = {"run_root": run_root}
        if judge_profile:
            judge_kw["profile_path"] = judge_profile
        if trader_profile:
            trader_kw["profile_path"] = trader_profile
        self.scout = Scout(self.ws)
        self.curator = Curator(self.ws)
        self.judge = Judge(self.ws, self.logger, **judge_kw)
        self.trader = Trader(self.ws, self.logger, **trader_kw)

    def _day(self, as_of: str) -> tuple[str, str]:
        """返回 (YYYYMMDD, YYYY-MM-DD)。"""
        if "-" in as_of:
            return as_of.replace("-", ""), as_of
        return as_of, f"{as_of[:4]}-{as_of[4:6]}-{as_of[6:]}"

    def _exists(self, rel: str) -> bool:
        return (self.ws.path(self.run_root) / rel).is_file()

    # ------------------------------------------------------------------ #
    # prepare：Scout（缺快照才跑）→ judge digest → Judge 任务包
    # ------------------------------------------------------------------ #

    def prepare(self, as_of: str) -> None:
        """日常第一步：数据 → Judge 交接包。幂等，可重跑。"""
        day, date_dash = self._day(as_of)
        snapshot = self.ws.path(f"data/snapshots/{date_dash}/snapshot.yaml")
        if not snapshot.is_file():
            print(f"[daily] 缺快照 {snapshot}，跑 Scout 全日链…")
            self.scout.run_date(day)
        else:
            print(f"[daily] 快照已存在，跳过 Scout：{snapshot}")

        if self._exists(f"{date_dash}/judge/digest.md"):
            print("[daily] judge digest 已存在，跳过渲染")
        else:
            self.curator.render_digest(
                day, module="judge", run_id=f"live-{day}-judge",
                output=f"{self.run_root}/{date_dash}/judge/digest.md")

        self.judge.prepare(date_dash)

    # ------------------------------------------------------------------ #
    # continue_：Judge.collect → trader digest → Trader 任务包
    # ------------------------------------------------------------------ #

    def continue_(self, as_of: str) -> None:
        """日常第二步：验收 review → Trader 交接包。幂等，可重跑。"""
        day, date_dash = self._day(as_of)
        self.judge.collect(date_dash)

        if self._exists(f"{date_dash}/trader/digest.md"):
            print("[daily] trader digest 已存在，跳过渲染")
        else:
            self.curator.render_digest(
                day, view=TRADER_VIEW, module="trader",
                run_id=f"live-{day}-trader",
                output=f"{self.run_root}/{date_dash}/trader/digest.md")

        self.trader.prepare(date_dash)

    # ------------------------------------------------------------------ #
    # settle：Trader.collect → Broker 回执 → Evaluator.score
    # （用户裁决 2026-10-29：Broker/Evaluator/Presenter 后续重新设计，
    #   暂时跳过，见 TODO「V4 模块重设计」）
    # ------------------------------------------------------------------ #

    def settle(self, as_of: str) -> None:
        """日常第三步：当前显式跳过，待 Broker/Evaluator 重设计后接通。"""
        _, date_dash = self._day(as_of)
        print(f"[daily] settle {date_dash} 跳过：Broker/Evaluator 待重设计后接通"
              "（TODO「V4 模块重设计」）")


__all__ = ["Daily"]
