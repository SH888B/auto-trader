from __future__ import annotations

import unittest

from scripts.evaluation.score_v3 import replay_buy


class ReplayBuyTest(unittest.TestCase):
    def action(self, **overrides):
        value = {
            "ts_code": "000001.SZ", "action": "buy", "order_type": "limit",
            "limit_price": 10.0, "stop_loss": 9.0, "take_profit": 11.0,
            "position_pct": 0.2,
        }
        value.update(overrides)
        return value

    def test_unfilled_limit_has_zero_pnl(self):
        bar = {"open": 12, "high": 12.5, "low": 11, "close": 12}
        result = replay_buy(self.action(), bar)
        self.assertEqual(result["status"], "unfilled")
        self.assertEqual(result["pnl_pct"], 0)

    def test_open_better_than_limit_is_entry(self):
        bar = {"open": 9.5, "high": 10.8, "low": 9.4, "close": 10.5}
        result = replay_buy(self.action(), bar)
        self.assertEqual(result["entry_price"], 9.5)
        self.assertEqual(result["exit_reason"], "close")

    def test_stop_has_priority_when_both_levels_touch(self):
        bar = {"open": 10, "high": 12, "low": 8, "close": 11}
        result = replay_buy(self.action(), bar)
        self.assertEqual(result["exit_reason"], "stop_loss")
        self.assertEqual(result["pnl_pct"], -10)

    def test_take_profit(self):
        bar = {"open": 10, "high": 11.5, "low": 9.5, "close": 11}
        result = replay_buy(self.action(), bar)
        self.assertEqual(result["exit_reason"], "take_profit")
        self.assertEqual(result["weighted_pnl_pct"], 2)

    def test_gap_stop_exits_at_open(self):
        bar = {"open": 8, "high": 10, "low": 7, "close": 9}
        result = replay_buy(self.action(), bar)
        self.assertEqual(result["exit_reason"], "gap_stop")
        self.assertEqual(result["pnl_pct"], 0)


if __name__ == "__main__":
    unittest.main()
