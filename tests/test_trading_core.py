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

    @patch.object(trading_core.time, "sleep")
    @patch.object(trading_core.time, "monotonic", side_effect=[0, 1])
    def test_close_cancels_stop_before_close(self, mocked_monotonic, mocked_sleep):
        stop = SimpleNamespace(id="stop-1")
        self.executor.client.get_orders.side_effect = [[stop], []]
        close_order = SimpleNamespace(id="close-1")
        self.executor.client.close_position.return_value = close_order

        self.assertTrue(self.executor.close_position("NVDA"))
        self.executor.client.cancel_order_by_id.assert_called_once_with("stop-1")
        self.executor.client.close_position.assert_called_once_with("NVDA")

    @patch.object(trading_core.time, "sleep")
    @patch.object(trading_core.time, "monotonic", side_effect=[0, 6])
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


if __name__ == "__main__":
    unittest.main()