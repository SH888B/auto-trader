"""取数账本与 raw 原子存储。

账本是 append-only JSONL（``data/meta/fetch_ledger.jsonl``），一行一次尝试，
与 ``logs/run_events.jsonl`` 同构：可 grep、可备份、不依赖数据库。

raw 保持现有格式（``{fields, items}``），只把写盘改成
``.tmp → fsync → os.replace``，避免中断留下半截 JSON；溯源信息（行数、
sha256、端点、尝试次数）全部记在账本里。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterator

from .sources import iso, now_tz


@dataclass(frozen=True)
class RawRef:
    """一次成功落盘的 raw 引用。"""

    path: str          # 工作区相对路径
    sha256: str
    rows: int


class RawStore:
    """``data/raw/<api>/<api>_<key>.json`` 的读写。"""

    def __init__(self, root: str | Path, rel_prefix: str = "data/raw") -> None:
        self.root = Path(root)
        self.rel_prefix = rel_prefix.rstrip("/")

    def path(self, api: str, key: str) -> Path:
        return self.root / api / f"{api}_{key}.json"

    def rel(self, api: str, key: str) -> str:
        return f"{self.rel_prefix}/{api}/{api}_{key}.json"

    def exists(self, api: str, key: str) -> bool:
        return self.path(api, key).is_file()

    def get(self, api: str, key: str) -> dict[str, Any] | None:
        """读取 payload；兼容未来的 envelope 格式。"""
        path = self.path(api, key)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "envelope_version" in data and "payload" in data:
            return data["payload"]
        return data

    def put(self, api: str, key: str, payload: dict[str, Any]) -> RawRef:
        """原子落盘：先写 ``.tmp``，fsync 后 ``os.replace``。"""
        path = self.path(api, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        data = json.dumps(payload, ensure_ascii=False)
        tmp.write_text(data, encoding="utf-8")
        with open(tmp, "rb+") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        rows = len(payload.get("items") or [])
        digest = hashlib.sha256(data.encode("utf-8")).hexdigest()
        return RawRef(path=self.rel(api, key), sha256=digest, rows=rows)


class FetchLedger:
    """append-only 取数账本。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        record = dict(record)
        record.setdefault("ts", now_tz().isoformat(timespec="seconds"))
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        return record

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def records(self) -> Iterator[dict[str, Any]]:
        if not self.path.is_file():
            return
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def for_date(self, trade_date: str | date) -> list[dict[str, Any]]:
        day = iso(trade_date)
        return [r for r in self.records() if r.get("trade_date") == day]

    def latest(self, trade_date: str | date | None = None) -> dict[str, dict[str, Any]]:
        """返回 ``source -> 最后一条记录``（按写入顺序，后写覆盖）。"""
        day = iso(trade_date) if trade_date else None
        out: dict[str, dict[str, Any]] = {}
        for record in self.records():
            if day and record.get("trade_date") != day:
                continue
            source = record.get("source")
            if source:
                out[str(source)] = record
        return out

    def latest_for(self, source: str, trade_date: str | date) -> dict[str, Any] | None:
        return self.latest(trade_date).get(source)

    def attempts(self, source: str, trade_date: str | date, revision: int) -> int:
        day = iso(trade_date)
        return sum(1 for r in self.records()
                   if r.get("source") == source and r.get("trade_date") == day
                   and int(r.get("revision", 1)) == int(revision))

    def ok_revisions(self, source: str, trade_date: str | date) -> list[int]:
        day = iso(trade_date)
        found = {int(r.get("revision", 1)) for r in self.records()
                 if r.get("source") == source and r.get("trade_date") == day
                 and r.get("status") == "ok"}
        return sorted(found)

    def first_ok_at(self, source: str, trade_date: str | date) -> str | None:
        day = iso(trade_date)
        for record in self.records():
            if (record.get("source") == source and record.get("trade_date") == day
                    and record.get("status") == "ok"):
                return str(record.get("ts"))
        return None

    def last_empty_pending(self, source: str, trade_date: str | date) -> dict[str, Any] | None:
        day = iso(trade_date)
        found = None
        for record in self.records():
            if (record.get("source") == source and record.get("trade_date") == day
                    and record.get("status") == "empty_pending"):
                found = record
        return found

    def count_since(self, day: str | date) -> int:
        """统计 ``ts`` 不早于 day 的尝试次数（配额守卫用）。"""
        start = iso(day)
        return sum(1 for r in self.records() if str(r.get("ts", ""))[:10] >= start)
