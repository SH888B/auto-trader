"""取数调度：按 ``sources.yaml`` 抓取、校验、落盘、记账，并计算就绪度。

与旧 ``fetch_full_date`` 的区别：

- 每个接口独立记录状态，失败不再"打印后继续"；
- 未到可用时间直接 ``not_due``，不调 API、不算缺口；
- 空响应分 ``empty_pending`` / ``empty_confirmed``，不再一落盘就永久跳过；
- raw 原子写，校验不过不落盘；
- 支持 deadline 与 per-API backoff，避免一个慢接口拖死整条链路。
"""
from __future__ import annotations

import random
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable

from .core import Workspace, dump_yaml, sha256_file
from .ledger import FetchLedger, RawRef, RawStore
from .relay import TushareClient
from .sources import (
    Sources,
    compute_readiness,
    now_tz,
    parse_date,
)

#: 账本里视为"已有可用数据"的终态
_SETTLED = frozenset({"ok", "empty_confirmed"})
#: 不再重试的终态
_FINAL = frozenset({"failed_final", "schema_bad"})


class FetchError(RuntimeError):
    """取数层的统一异常。"""


class FetchIncomplete(FetchError):
    """仍有 block 级接口未就绪。"""

    def __init__(self, blocked: list[str], readiness: dict[str, Any]) -> None:
        self.blocked = blocked
        self.readiness = readiness
        super().__init__(f"数据未就绪（blocked）：{', '.join(blocked)}")


def classify_error(exc: BaseException) -> tuple[str, bool]:
    """把异常映射成 ``(error_class, retryable)``。"""
    message = str(exc)
    lowered = message.lower()
    if "40101" in message:
        return "unsupported", False
    if "429" in message or "限速" in message:
        return "rate_limit", True
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout", True
    if "invalid_params" in message:
        return "invalid_params", True
    if "积分" in message or "permission" in lowered:
        return "quota", False
    return "relay", True


class Fetcher:
    """按契约抓取单个交易日的数据。"""

    def __init__(self, workspace: Workspace | None = None,
                 sources: Sources | None = None,
                 ledger: FetchLedger | None = None,
                 raw: RawStore | None = None,
                 client_factory: Callable[[], Any] | None = None,
                 clock: Callable[[], datetime] | None = None,
                 sleep: Callable[[float], None] | None = None) -> None:
        self.ws = workspace or Workspace()
        self.sources = sources or Sources.load(self.ws.path("configs/data/sources.yaml"))
        self.ledger = ledger or FetchLedger(self.ws.path("data/meta/fetch_ledger.jsonl"))
        self.raw = raw or RawStore(self.ws.path("data/raw"))
        self._client_factory = client_factory or (
            lambda: TushareClient(pace=1.5, max_retries=4, timeout=60))
        self._client: Any | None = None
        self.clock = clock or now_tz
        self.sleep = sleep or time.sleep

    # ------------------------------------------------------------------ #
    # 客户端
    # ------------------------------------------------------------------ #
    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def _last_endpoint(self) -> str | None:
        log = getattr(self.client, "call_log", None) or []
        return log[-1].get("endpoint") if log else None

    # ------------------------------------------------------------------ #
    # 单接口
    # ------------------------------------------------------------------ #
    def _call(self, name: str, params: dict[str, str]) -> dict[str, Any]:
        source = self.sources[name]
        if source.page:
            fields, items = self.client.call_paginated(
                source.api, params,
                page_size=int(source.page.get("size", 5000)),
                max_pages=int(source.page.get("max_pages", 4)),
            )
            return {"fields": fields, "items": items}
        data = self.client.call(source.api, params,
                                timeout=source.retry.get("read_timeout"))
        body = data.get("data") or {}
        return {"fields": body.get("fields") or [], "items": body.get("items") or []}

    def _record(self, name: str, trade_date: str, revision: int, attempt: int,
                status: str, *, rows: int = 0, error_class: str | None = None,
                error: str | None = None, raw_ref: RawRef | None = None,
                run_id: str | None = None, duration_ms: int | None = None,
                moment: datetime | None = None) -> dict[str, Any]:
        source = self.sources[name]
        record = {
            "run_id": run_id, "source": name, "api": source.api,
            "trade_date": trade_date, "revision": revision, "attempt": attempt,
            "status": status, "rows": rows,
            "endpoint": self._last_endpoint(),
            "duration_ms": duration_ms,
            "error_class": error_class, "error": error,
            "raw_path": raw_ref.path if raw_ref else None,
            "sha256": raw_ref.sha256 if raw_ref else None,
            "key": source.key(trade_date),
        }
        if moment is not None:
            record["ts"] = moment.isoformat(timespec="seconds")
        return self.ledger.append(record)

    def fetch_one(self, name: str, trade_date: str | date, *,
                  revision: int | None = None, run_id: str | None = None,
                  force: bool = False) -> dict[str, Any]:
        """抓一次（单次尝试），返回账本记录。

        ``force`` 只绕过"未到可用时间"，不绕过校验。
        """
        source = self.sources[name]
        day = parse_date(trade_date)
        day_iso = day.isoformat()
        moment = self.clock()
        available = source.available_at(day)

        if moment < available and not force:
            return {"source": name, "trade_date": day_iso, "status": "not_due",
                    "available_at": available.isoformat(timespec="seconds"), "rows": 0}

        oks = self.ledger.ok_revisions(name, day_iso)
        resolved = int(revision) if revision else (max(oks) if oks else 1)
        attempt = self.ledger.attempts(name, day_iso, resolved) + 1
        started = self.clock()

        try:
            payload = self._call(name, source.params_for(day))
        except Exception as exc:  # noqa: BLE001 - 统一分类后记账
            error_class, retryable = classify_error(exc)
            status = "failed" if retryable else "failed_final"
            return self._record(name, day_iso, resolved, attempt, status,
                                error_class=error_class, error=str(exc)[:300],
                                run_id=run_id, moment=moment,
                                duration_ms=int((self.clock() - started).total_seconds() * 1000))

        rows = len(payload.get("items") or [])
        errors = self.sources.validate(name, payload)
        duration = int((self.clock() - started).total_seconds() * 1000)

        if rows == 0 and source.expect.allow_empty:
            confirm_at = available + timedelta(minutes=self.sources.confirm_gap_minutes)
            last_pending = self.ledger.last_empty_pending(name, day_iso)
            if moment < confirm_at or last_pending is None:
                return self._record(name, day_iso, resolved, attempt, "empty_pending",
                                    rows=0, error_class="empty",
                                    run_id=run_id, duration_ms=duration, moment=moment)
            ref = None if oks else self.raw.put(source.api, source.key(day), payload)
            return self._record(name, day_iso, resolved, attempt, "empty_confirmed",
                                rows=0, error_class="empty", raw_ref=ref,
                                run_id=run_id, duration_ms=duration, moment=moment)

        if errors:
            return self._record(name, day_iso, resolved, attempt, "schema_bad",
                                rows=rows, error_class="schema",
                                error="; ".join(errors)[:300],
                                run_id=run_id, duration_ms=duration, moment=moment)

        ref = self.raw.put(source.api, source.key(day), payload)
        return self._record(name, day_iso, resolved, attempt, "ok",
                            rows=rows, raw_ref=ref, run_id=run_id,
                            duration_ms=duration, moment=moment)

    # ------------------------------------------------------------------ #
    # 调度
    # ------------------------------------------------------------------ #
    def sync(self, trade_date: str | date, *, window: str = "all",
             deadline_minutes: float = 30.0, max_attempts: int | None = None,
             force: bool = False, run_id: str | None = None,
             only: list[str] | None = None) -> dict[str, Any]:
        """在一个窗口内把该抓的接口抓到终态。

        Returns:
            ``{date, window, results, readiness}``；``results[name]`` 是最后一条账本记录。
        """
        day = parse_date(trade_date)
        day_iso = day.isoformat()
        names = only or self.sources.in_window(window)
        deadline = self.clock() + timedelta(minutes=deadline_minutes)
        results: dict[str, dict[str, Any]] = {}

        for name in names:
            if name not in self.sources:
                raise FetchError(f"sources.yaml 未声明接口：{name}")
            source = self.sources[name]
            last = self.ledger.latest_for(name, day_iso)
            if last and last.get("status") in _SETTLED and not force:
                results[name] = {**last, "skipped": True}
                continue

            attempts = int(max_attempts or source.retry.get("attempts", 4))
            for index in range(attempts):
                record = self.fetch_one(name, day_iso, force=force, run_id=run_id)
                results[name] = record
                if record["status"] in _SETTLED or record["status"] in _FINAL \
                        or record["status"] == "not_due":
                    break
                backoff = source.retry.get("backoff") or [15, 45, 120, 300]
                delay = float(backoff[min(index, len(backoff) - 1)])
                delay *= 1 + random.uniform(-float(source.retry.get("jitter", 0.25)),
                                            float(source.retry.get("jitter", 0.25)))
                if self.clock() + timedelta(seconds=delay) > deadline:
                    break
                self.sleep(delay)

        readiness = self.status(day_iso)
        return {"date": day_iso, "window": window,
                "results": results, "readiness": readiness}

    def sync_many(self, dates: list[str | date], *, window: str = "all",
                  deadline_minutes: float = 60.0, force: bool = False,
                  only: list[str] | None = None) -> list[dict[str, Any]]:
        """逐日 sync；单日失败不阻塞其余日期。"""
        out = []
        for value in dates:
            out.append(self.sync(value, window=window,
                                 deadline_minutes=deadline_minutes,
                                 force=force, only=only))
        return out

    # ------------------------------------------------------------------ #
    # 就绪度与对账
    # ------------------------------------------------------------------ #
    def status(self, trade_date: str | date) -> dict[str, Any]:
        """折叠账本 → 就绪度，并写 ``data/meta/readiness/<date>.yaml``。"""
        day = parse_date(trade_date)
        day_iso = day.isoformat()
        moment = self.clock()
        latest = self.ledger.latest(day_iso)
        states: dict[str, dict[str, Any]] = {}

        for name, source in self.sources.items():
            record = latest.get(name)
            key = source.key(day)
            if record:
                revision = int(record.get("revision", 1))
                states[name] = {
                    "state": record.get("status"),
                    "rows": record.get("rows"),
                    "revision": revision,
                    "attempts": self.ledger.attempts(name, day_iso, revision),
                    "first_ok_at": self.ledger.first_ok_at(name, day_iso),
                    "available_at": source.available_at(day).isoformat(timespec="seconds"),
                }
            elif self.raw.exists(source.api, key):
                payload = self.raw.get(source.api, key) or {}
                rows = len(payload.get("items") or [])
                if rows == 0 and source.expect.allow_empty:
                    state = "empty_confirmed" if source.expect.empty_is_gap else "ok"
                else:
                    state = "ok"
                states[name] = {
                    "state": state,
                    "rows": rows,
                    "legacy": True,
                    "available_at": source.available_at(day).isoformat(timespec="seconds"),
                }
            else:
                states[name] = {
                    "state": "not_due" if moment < source.available_at(day) else "missing",
                    "available_at": source.available_at(day).isoformat(timespec="seconds"),
                }

        readiness = compute_readiness(day_iso, self.sources, states, moment)
        target = self.ws.path(f"data/meta/readiness/{day_iso}.yaml")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dump_yaml(readiness.to_dict()), encoding="utf-8")
        return readiness.to_dict()

    def verify(self, trade_date: str | date) -> list[str]:
        """账本 vs raw 对账：sha256、行数、缺失文件、孤儿文件。"""
        day = parse_date(trade_date)
        day_iso = day.isoformat()
        issues: list[str] = []
        latest = self.ledger.latest(day_iso)
        has_ledger = bool(self.ledger.for_date(day_iso))

        for name, source in self.sources.items():
            record = latest.get(name)
            key = source.key(day)
            if not record:
                if has_ledger and self.raw.exists(source.api, key):
                    issues.append(f"{name}: raw 存在但账本无记录（可能是旧数据）")
                continue
            if record.get("status") != "ok":
                continue
            rel = record.get("raw_path")
            if not rel:
                issues.append(f"{name}: ok 记录缺少 raw_path")
                continue
            path = self.ws.path(rel)
            if not path.is_file():
                issues.append(f"{name}: 账本 ok 但 raw 缺失（{rel}）")
                continue
            if sha256_file(path) != record.get("sha256"):
                issues.append(f"{name}: raw sha256 与账本不符（{rel}）")
                continue
            payload = self.raw.get(source.api, key) or {}
            rows = len(payload.get("items") or [])
            if rows != record.get("rows"):
                issues.append(f"{name}: raw 行数 {rows} ≠ 账本 {record.get('rows')}")
        return issues
