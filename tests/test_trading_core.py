import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from alpaca.trading.enums import OrderSide, OrderStatus, OrderType

import trading_core


class TradeExecutorTests(unittest.TestCase):
    def setUp(self):
        self.executor = object.__new__(trading_core.TradeExecutor)
        self.executor.client = Mock()
        self.executor._symbol_locks = {}
        self.executor._symbol_locks_guard = __import__("threading").Lock()
        self.executor._last_symbol_action = {}

    @patch.object(trading_core.time, "sleep")
    @patch.object(trading_core.time, "monotonic", side_effect=[0, 1, 2])
    def test_close_cancels_stop_before_close(self, mocked_monotonic, mocked_sleep):
        stop = SimpleNamespace(id="stop-1")
        self.executor.client.get_orders.side_effect = [[stop], []]
        close_order = SimpleNamespace(id="close-1")
        self.executor.client.close_position.return_value = close_order

        self.assertTrue(self.executor.close_position("NVDA"))
        self.executor.client.cancel_order_by_id.assert_called_once_with("stop-1")
        self.executor.client.close_position.assert_called_once_with("NVDA")

    @patch.object(trading_core.time, "sleep")
    @patch.object(trading_core.time, "monotonic", side_effect=[0, 6, 7])
    def test_close_does_not_submit_while_order_remains_open(self, mocked_monotonic, mocked_sleep):
        stop = SimpleNamespace(id="stop-1")
        self.executor.client.get_orders.return_value = [stop]

        self.assertFalse(self.executor.close_position("QQQ"))
        self.executor.client.close_position.assert_not_called()

    def test_reconcile_places_missing_long_protection(self):
        position = SimpleNamespace(symbol="AAPL", qty="35.5")
        self.executor.get_open_positions = Mock(return_value=[position])
        self.executor.client.get_orders.return_value = []
        self.executor._place_trailing_stop_with_retry = Mock(return_value="stop-2")

        self.executor.reconcile_orders()

        self.executor._place_trailing_stop_with_retry.assert_called_once_with(
            "AAPL", OrderSide.SELL, trading_core.config.DEFAULT_STOP_LOSS_PCT, qty=35,
        )

    def test_reversal_cooldown_blocks_same_symbol_entry(self):
        now = 1000.0
        self.executor._last_symbol_action = {"AAPL": now - 10}

        with patch.object(trading_core.time, "monotonic", return_value=now):
            self.assertFalse(self.executor.can_trade_symbol("AAPL"))

        self.executor._last_symbol_action["AAPL"] = now - (trading_core.config.REVERSAL_COOLDOWN_SECONDS + 5)
        with patch.object(trading_core.time, "monotonic", return_value=now):
            self.assertTrue(self.executor.can_trade_symbol("AAPL"))

    def test_effective_capital_gate_blocks_over_deployed_trade(self):
        account = SimpleNamespace(cash="1000.0", buying_power="245.0")
        self.executor.get_account = Mock(return_value=account)

        self.assertTrue(self.executor.has_effective_capital_for_trade(200.0))
        self.assertFalse(self.executor.has_effective_capital_for_trade(300.0))

    def test_trade_state_monitor_reports_pending_orders_and_cooldowns(self):
        self.executor.get_open_positions = Mock(return_value=[SimpleNamespace(symbol="AAPL", qty="5.0")])
        self.executor.client.get_orders.return_value = [
            SimpleNamespace(symbol="AAPL", id="order-1", side="buy", type="market"),
            SimpleNamespace(symbol="NVDA", id="order-2", side="sell", type="trailing_stop"),
        ]
        self.executor._last_symbol_action = {"AAPL": 190.0}
        self.executor.get_account = Mock(return_value=SimpleNamespace(cash="5000.0", buying_power="2000.0"))

        with patch.object(trading_core.time, "monotonic", return_value=200.0):
            summary = self.executor.get_trade_state_summary()

        self.assertIn("open_positions", summary)
        self.assertIn("pending_orders", summary)
        self.assertIn("symbol_cooldowns", summary)
        self.assertEqual(summary["pending_orders"]["AAPL"][0]["id"], "order-1")
        self.assertIn("AAPL", summary["symbol_cooldowns"])


if __name__ == "__main__":
    unittest.main()