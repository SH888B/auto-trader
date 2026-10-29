"""数据源契约：加载 ``configs/data/sources.yaml``，并提供可用时点/校验/就绪度计算。

设计要点（见 DATA-ROBUSTNESS-PLAN.md §3.2–3.4）：

- 每个接口的"怎么调、期望什么、何时可用、缺了算几级"都在 YAML 里，代码不再硬编码。
- ``available_at`` 用固定 +08:00（Windows 上 zoneinfo 可能缺 tzdata，本项目不需要 DST）。
- 就绪度由账本状态折叠而来：``not_due`` 不算缺口，``empty_confirmed`` 才算。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import yaml

#: 中国标准时间；固定偏移即可（无夏令时），避免依赖 tzdata。
TZ = timezone(timedelta(hours=8))

#: 就绪度状态
READY, DEGRADED, BLOCKED = "ready", "degraded", "blocked"

#: 视为"数据已就位"的账本状态
OK_STATES = frozenset({"ok"})


def parse_date(value: str | date | datetime) -> date:
    """把 ``YYYYMMDD`` / ``YYYY-MM-DD`` / date 统一成 ``date``。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().replace("-", "")
    if len(text) != 8 or not text.isdigit():
        raise ValueError(f"日期必须是 YYYYMMDD 或 YYYY-MM-DD：{value!r}")
    return date(int(text[:4]), int(text[4:6]), int(text[6:]))


def iso(value: str | date | datetime) -> str:
    """统一输出 ``YYYY-MM-DD``。"""
    return parse_date(value).isoformat()


def parse_hhmm(text: str) -> time:
    """解析 ``"08:30"`` / ``"8:30"``。"""
    hour, _, minute = str(text).partition(":")
    return time(int(hour), int(minute or 0))


def now_tz() -> datetime:
    """当前 +08:00 时间（测试可注入）。"""
    return datetime.now(TZ)


@dataclass(frozen=True)
class Expect:
    """一份响应的完整性期望。"""

    min_rows: int = 1
    max_rows: int | None = None
    fields: tuple[str, ...] = ()
    allow_empty: bool = False
    #: 空响应是否算缺口（kpl 次日增强=是；跌停池为空=合法结果，否）
    empty_is_gap: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Expect":
        data = data or {}
        return cls(
            min_rows=int(data.get("min_rows", 1)),
            max_rows=int(data["max_rows"]) if data.get("max_rows") else None,
            fields=tuple(data.get("fields") or ()),
            allow_empty=bool(data.get("allow_empty", False)),
            empty_is_gap=bool(data.get("empty_is_gap", False)),
        )


@dataclass(frozen=True)
class Avail:
    """该交易日的数据最早何时可用。"""

    day_offset: int = 0
    after: time = time(17, 0)
    confidence: str = "doc"

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Avail":
        data = data or {}
        return cls(
            day_offset=int(data.get("day_offset", 0)),
            after=parse_hhmm(data.get("after", "17:00")),
            confidence=str(data.get("confidence", "doc")),
        )


@dataclass(frozen=True)
class Source:
    """一个可抓取的数据源（通常对应一次 API 调用）。"""

    name: str
    api: str
    window: str
    key_template: str
    params: dict[str, str]
    expect: Expect
    avail: Avail
    klass: str
    page: dict[str, Any] | None = None
    retry: dict[str, Any] = field(default_factory=dict)
    endpoint: str | None = None

    @property
    def is_block(self) -> bool:
        return self.klass == "block"

    @property
    def is_enrich(self) -> bool:
        return self.klass == "enrich"

    def key(self, trade_date: str | date) -> str:
        return self.key_template.format(date=parse_date(trade_date).strftime("%Y%m%d"))

    def params_for(self, trade_date: str | date) -> dict[str, str]:
        return {k: str(v).format(date=parse_date(trade_date).strftime("%Y%m%d"))
                for k, v in self.params.items()}

    def available_at(self, trade_date: str | date) -> datetime:
        """该交易日这一份数据的最早可用时刻（+08:00）。"""
        day = parse_date(trade_date) + timedelta(days=self.avail.day_offset)
        return datetime.combine(day, self.avail.after, tzinfo=TZ)


class Sources:
    """``sources.yaml`` 的只读视图。"""

    def __init__(self, data: dict[str, Any] | None) -> None:
        data = data or {}
        self.version = str(data.get("version", "?"))
        defaults = data.get("defaults") or {}
        self.default_retry: dict[str, Any] = dict(defaults.get("retry") or {})
        self.confirm_gap_minutes: int = int(defaults.get("confirm_gap_minutes", 10))
        self._sources: dict[str, Source] = {}
        for name, raw in (data.get("apis") or {}).items():
            if not isinstance(raw, dict):
                raise ValueError(f"sources.yaml 中 {name} 必须是映射")
            retry = dict(self.default_retry)
            retry.update(raw.get("retry") or {})
            self._sources[name] = Source(
                name=name,
                api=str(raw.get("api") or name),
                window=str(raw.get("window", "evening")),
                key_template=str(raw.get("key", "{date}")),
                params=dict(raw.get("params") or {}),
                expect=Expect.from_dict(raw.get("expect")),
                avail=Avail.from_dict(raw.get("avail")),
                klass=str(raw.get("class", "optional")),
                page=raw.get("page"),
                retry=retry,
                endpoint=raw.get("endpoint"),
            )
        if not self._sources:
            raise ValueError("sources.yaml 未声明任何接口")

    @classmethod
    def load(cls, path: str | Path) -> "Sources":
        target = Path(path)
        return cls(yaml.safe_load(target.read_text(encoding="utf-8")) or {})

    def __getitem__(self, name: str) -> Source:
        return self._sources[name]

    def __contains__(self, name: object) -> bool:
        return name in self._sources

    def __iter__(self) -> Iterator[str]:
        return iter(self._sources)

    def __len__(self) -> int:
        return len(self._sources)

    def items(self):
        return self._sources.items()

    def names(self) -> list[str]:
        return list(self._sources)

    def in_window(self, window: str) -> list[str]:
        """返回某个调度窗口的接口名；``all`` 表示全部。"""
        if window in ("", "all", None):
            return self.names()
        return [n for n, s in self._sources.items() if s.window == window]

    def validate(self, name: str, payload: dict[str, Any]) -> list[str]:
        """校验一份响应是否完整；返回错误清单（空 = 通过）。

        空响应：``allow_empty`` 时不算错（由 Fetch 层决定 pending/confirmed），
        否则记一条 ``empty`` 错误。
        """
        source = self[name]
        expect = source.expect
        items = payload.get("items") or []
        fields = list(payload.get("fields") or [])
        if not items:
            return [] if expect.allow_empty else ["empty（接口未返回任何行）"]
        errors: list[str] = []
        if len(items) < expect.min_rows:
            errors.append(f"rows={len(items)} < min_rows={expect.min_rows}")
        if expect.max_rows and len(items) > expect.max_rows:
            errors.append(f"rows={len(items)} > max_rows={expect.max_rows}")
        missing = [f for f in expect.fields if f not in fields]
        if missing:
            errors.append(f"缺少字段 {missing}")
        pk = (source.page or {}).get("pk") or []
        if pk and all(f in fields for f in pk):
            idx = [fields.index(f) for f in pk]
            seen: set[tuple] = set()
            for row in items:
                key = tuple(row[i] if i < len(row) else None for i in idx)
                if key in seen:
                    errors.append(f"主键重复 {key}")
                    break
                seen.add(key)
        return errors


@dataclass
class Readiness:
    """某决策日的数据就绪度。"""

    trade_date: str
    status: str
    blocking: list[str] = field(default_factory=list)
    degraded_by: list[str] = field(default_factory=list)
    detail: dict[str, dict[str, Any]] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)
    decided_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "readiness_version": "r1.0",
            "trade_date": self.trade_date,
            "status": self.status,
            "blocking": self.blocking,
            "degraded_by": self.degraded_by,
            "gaps": self.gaps,
            "detail": self.detail,
            "decided_at": self.decided_at,
        }

    def summary(self) -> str:
        """一行摘要，CLI 用。"""
        return (f"{self.trade_date} status={self.status} "
                f"blocking={self.blocking or '-'} degraded_by={self.degraded_by or '-'}")


def compute_readiness(trade_date: str | date, sources: Sources,
                      states: dict[str, dict[str, Any]],
                      now: datetime | None = None) -> Readiness:
    """把各接口状态折叠成就绪度。

    规则：
        - ``ok`` / 文件回退 = 就位；
        - ``not_due`` = 还没到时间，**不算缺口**（这正是当前系统最大的误报源）；
        - ``empty_pending`` = 等二次确认，不算缺口；
        - ``empty_confirmed`` / ``failed`` / ``failed_final`` / ``schema_bad`` / ``missing``
          对 ``block`` 记为 blocking，对 ``enrich`` 记为 degraded；
        - ``optional`` 只记录，不影响状态。
    """
    day = iso(trade_date)
    moment = now or now_tz()
    detail: dict[str, dict[str, Any]] = {}
    blocking: list[str] = []
    degraded: list[str] = []
    gaps: list[str] = []

    for name, source in sources.items():
        state = dict(states.get(name) or {"state": "missing"})
        state_name = str(state.get("state", "missing"))
        state["class"] = source.klass
        detail[name] = state

        if state_name in OK_STATES:
            continue
        if state_name == "not_due":
            continue
        if state_name == "empty_pending":
            continue
        if state_name == "empty_confirmed" and not source.expect.empty_is_gap:
            continue
        # optional 接口缺失不影响就绪度，也不进 gaps（避免"每天都缺 daily_basic"式噪声）
        if not (source.is_block or source.is_enrich):
            continue
        gaps.append(f"{name}: {state_name}")
        if source.is_block:
            blocking.append(name)
        elif source.is_enrich:
            degraded.append(name)

    if blocking:
        status = BLOCKED
    elif degraded:
        status = DEGRADED
    else:
        status = READY

    return Readiness(
        trade_date=day,
        status=status,
        blocking=blocking,
        degraded_by=degraded,
        detail=detail,
        gaps=gaps,
        decided_at=moment.isoformat(timespec="seconds"),
    )
