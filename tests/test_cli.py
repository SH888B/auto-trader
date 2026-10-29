# -*- coding: utf-8 -*-
"""CLI 测试：参数解析 → 正确的 Scout/Curator 方法分发。"""
from __future__ import annotations

import pytest

import autotrader.cli as cli


class _Recorder:
    """记录调用参数的 Scout/Curator 替身。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, name: str, *args, **kwargs) -> None:
        self.calls.append((name, args, kwargs))

    def __getattr__(self, name: str):  # noqa: ANN204 - 测试替身，任意方法都记录
        return lambda *a, **k: self._record(name, *a, **k)


@pytest.fixture()
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(cli, "_scout_run", cli._scout_run)  # 保持原样
    # handler 内部是延迟 import，需拦截模块级类
    import autotrader.curator  # noqa: F401
    import autotrader.scout  # noqa: F401
    monkeypatch.setattr("autotrader.scout.Scout", lambda: rec)
    monkeypatch.setattr("autotrader.curator.Curator", lambda: rec)
    return rec


class TestTopLevel:
    def test_no_module_prints_help(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main([]) == 0
        assert "auto-trader" in capsys.readouterr().out

    def test_version(self) -> None:
        with pytest.raises(SystemExit) as exc:
            cli.main(["--version"])
        assert exc.value.code == 0


class TestScoutDispatch:
    def test_run(self, recorder: _Recorder) -> None:
        assert cli.main(["scout", "run", "--date", "20261029"]) == 0
        assert recorder.calls == [("run_date", ("20261029",), {"auto_fetch": True})]

    def test_run_no_auto_fetch(self, recorder: _Recorder) -> None:
        cli.main(["scout", "run", "--date", "20261029", "--no-auto-fetch"])
        assert recorder.calls[-1][2]["auto_fetch"] is False

    def test_fetch(self, recorder: _Recorder) -> None:
        cli.main(["scout", "fetch", "--dates", "20261028", "20261029", "--light"])
        assert recorder.calls == [
            ("fetch_daily", (["20261028", "20261029"],), {"light": True, "force": False}),
        ]

    def test_load_date(self, recorder: _Recorder) -> None:
        cli.main(["scout", "load", "--date", "20261029"])
        assert recorder.calls == [("load_warehouse", (), {"date": "20261029",
                                                           "all_dates": False})]

    def test_load_all(self, recorder: _Recorder) -> None:
        cli.main(["scout", "load", "--ingest-all"])
        assert recorder.calls == [("load_warehouse", (), {"all_dates": True})]

    def test_select_finalize_build(self, recorder: _Recorder) -> None:
        cli.main(["scout", "select", "--date", "20261029"])
        cli.main(["scout", "finalize", "--date", "20261029"])
        cli.main(["scout", "build", "--date", "20261029"])
        assert [c[0] for c in recorder.calls] == ["select_stocks", "finalize",
                                                  "build_snapshot"]

    def test_catchup(self, recorder: _Recorder) -> None:
        cli.main(["scout", "catchup", "--start", "20261001", "--end", "20261029",
                  "--check"])
        assert recorder.calls == [
            ("catchup", ("20261001", "20261029"), {"check": True, "replay": False}),
        ]

    def test_members_boards_split(self, recorder: _Recorder) -> None:
        cli.main(["scout", "members", "--date", "20261029", "--boards", " 881121,885531 "])
        assert recorder.calls == [
            ("fetch_theme_members", ("20261029",),
             {"boards": ["881121", "885531"], "max_age_days": 7.0}),
        ]

    def test_members_default_boards(self, recorder: _Recorder) -> None:
        cli.main(["scout", "members", "--date", "20261029"])
        assert recorder.calls[-1][2]["boards"] is None


class TestCuratorDispatch:
    def test_digest_defaults(self, recorder: _Recorder) -> None:
        cli.main(["curator", "digest", "--date", "20261029"])
        assert recorder.calls == [
            ("render_digest", ("20261029",),
             {"view": "configs/digest/judge_default.yaml", "run_id": None,
              "module": "judge", "output": None}),
        ]

    def test_digest_trader(self, recorder: _Recorder) -> None:
        cli.main(["curator", "digest", "--date", "20261029", "--module", "trader",
                  "--run-id", "live-x"])
        assert recorder.calls[-1][2]["module"] == "trader"
        assert recorder.calls[-1][2]["run_id"] == "live-x"

    def test_watchlist_apply(self, recorder: _Recorder) -> None:
        cli.main(["curator", "watchlist", "apply",
                  "--review", "runs/live/x/judge/review.json",
                  "--target", "state/watchlist/judge/current.yaml"])
        assert recorder.calls == [
            ("apply_watchlist_updates",
             ("runs/live/x/judge/review.json", "state/watchlist/judge/current.yaml"),
             {"snapshot": None}),
        ]


class TestPlaceholderModules:
    @pytest.mark.parametrize("module", ["daily", "validate", "score", "logs"])
    def test_returns_2(self, module: str,
                       capsys: pytest.CaptureFixture) -> None:
        assert cli.main([module]) == 2
        assert "尚未迁移完成" in capsys.readouterr().out
