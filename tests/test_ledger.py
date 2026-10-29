"""账本与 raw 原子存储单元测试。"""
from __future__ import annotations

import json
from pathlib import Path

from autotrader.ledger import FetchLedger, RawStore


def test_ledger_append_and_fold(tmp_path: Path) -> None:
    ledger = FetchLedger(tmp_path / "ledger.jsonl")
    ledger.append({"source": "daily", "trade_date": "2026-10-28", "revision": 1,
                   "attempt": 1, "status": "failed", "rows": 0, "ts": "2026-10-28T16:00:00+08:00"})
    ledger.append({"source": "daily", "trade_date": "2026-10-28", "revision": 1,
                   "attempt": 2, "status": "ok", "rows": 5570,
                   "ts": "2026-10-28T16:05:00+08:00"})
    ledger.append({"source": "kpl_list", "trade_date": "2026-10-28", "revision": 1,
                   "attempt": 1, "status": "empty_pending", "rows": 0,
                   "ts": "2026-10-29T08:31:00+08:00"})

    latest = ledger.latest("2026-10-28")
    assert latest["daily"]["status"] == "ok"
    assert latest["kpl_list"]["status"] == "empty_pending"
    assert ledger.attempts("daily", "2026-10-28", 1) == 2
    assert ledger.ok_revisions("daily", "2026-10-28") == [1]
    assert ledger.first_ok_at("daily", "2026-10-28") == "2026-10-28T16:05:00+08:00"
    assert ledger.last_empty_pending("kpl_list", "2026-10-28")["attempt"] == 1


def test_ledger_tolerates_torn_line(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    path.write_text('{"source":"daily","status":"ok"}\n{broken\n', encoding="utf-8")
    records = list(FetchLedger(path).records())
    assert len(records) == 1


def test_raw_store_atomic_write(tmp_path: Path) -> None:
    store = RawStore(tmp_path / "raw")
    payload = {"fields": ["ts_code"], "items": [["000001.SZ"], ["600000.SH"]]}
    ref = store.put("daily", "20261028", payload)

    assert store.path("daily", "20261028").is_file()
    assert not list((tmp_path / "raw" / "daily").glob("*.tmp")), "不应残留 .tmp"
    assert ref.rows == 2
    assert ref.path == "data/raw/daily/daily_20261028.json"
    assert store.get("daily", "20261028") == payload

    data = json.loads((tmp_path / "raw" / "daily" / "daily_20261028.json")
                      .read_text(encoding="utf-8"))
    assert data["items"][0] == ["000001.SZ"]


def test_raw_store_reads_future_envelope(tmp_path: Path) -> None:
    path = tmp_path / "raw" / "daily" / "daily_20261028.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"envelope_version": "raw1.0",
                                "payload": {"fields": [], "items": []}}),
                    encoding="utf-8")
    assert RawStore(tmp_path / "raw").get("daily", "20261028") == {
        "fields": [], "items": []}


def test_raw_store_overwrites_atomically(tmp_path: Path) -> None:
    store = RawStore(tmp_path / "raw")
    store.put("daily", "20261028", {"fields": [], "items": [["A"]]})
    ref = store.put("daily", "20261028", {"fields": [], "items": [["A"], ["B"]]})
    assert ref.rows == 2
    assert store.get("daily", "20261028")["items"] == [["A"], ["B"]]
