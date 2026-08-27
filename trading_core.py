"""
trading_core.py -- Shared execution layer.

Wraps the Alpaca paper trading API: connection, position sizing,
market order placement, and trailing-stop protection.

Credentials are read ONLY from environment variables (ALPACA_API_KEY,
ALPACA_SECRET_KEY). This module never accepts keys as arguments or
literals -- if they're missing, it fails loudly rather than silently
trying something insecure.
"""

import logging
import math
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, TrailingStopOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType, OrderStatus, QueryOrderStatus

import config

logger = logging.getLogger("trading_core")

# How long to wait for an order to reach a fill-like terminal state before
# giving up and treating it as unresolved. Alpaca's paper engine usually
# fills market orders in well under a second, but polling immediately
# after submission (0 wait) was the root cause of every stop-placement
# and safety-net failure seen in production logs -- every single trade
# raced ahead of the fill.
ORDER_FILL_POLL_INTERVAL_SECONDS = 0.3
ORDER_FILL_TIMEOUT_SECONDS = 10
STOP_PLACEMENT_MAX_ATTEMPTS = 3
STOP_PLACEMENT_RETRY_DELAY_SECONDS = 1.0
ORDER_CANCEL_TIMEOUT_SECONDS = 5

FILLED_STATUSES = {OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED}


class ConfigError(RuntimeError):
    """Raised when required configuration/credentials are missing."""


@dataclass
class TradeResult:
    success: bool          # entry order filled
    protected: bool         # trailing stop is actually live on this position
    symbol: str
    side: str
    qty: Optional[float] = None
    notional: Optional[float] = None
    order_id: Optional[str] = None
    stop_order_id: Optional[str] = None
    error: Optional[str] = None


class TradeExecutor:
    """
    Shared trade executor used by any signal source (local indicator engine,
    webhook-driven bot, etc). Handles:
      - connecting to Alpaca paper trading
      - position sizing based on directional % rules
      - placing entry orders (market, notional for longs, whole-share for shorts)
      - placing a GTC trailing stop immediately after entry
      - the "unprotected position closure" safety net if the stop fails
    """

    def __init__(self):
        if not config.ALPACA_API_KEY or not config.ALPACA_SECRET_KEY:
            raise ConfigError(
                "ALPACA_API_KEY / ALPACA_SECRET_KEY environment variables are not set. "
                "Set them in your shell profile before running the bot -- see README.md."
            )
        self.client = TradingClient(
            config.ALPACA_API_KEY,
            config.ALPACA_SECRET_KEY,
            paper=config.ALPACA_PAPER,
        )
        self._symbol_locks = {}
        self._symbol_locks_guard = threading.Lock()
        logger.info("Connected to Alpaca (%s)", "paper" if config.ALPACA_PAPER else "LIVE")

    # ------------------------------------------------------------------
    # Account / position helpers
    # ------------------------------------------------------------------

    def get_account(self):
        return self.client.get_account()

    def get_clock(self):
        return self.client.get_clock()

    @contextmanager
    def symbol_lock(self, symbol: str):
        with self._symbol_locks_guard:
            lock = self._symbol_locks.setdefault(symbol, threading.Lock())
        with lock:
            yield

    def get_open_positions(self):
        return self.client.get_all_positions()

    def get_position(self, symbol: str):
        try:
            return self.client.get_open_position(symbol)
        except Exception:
            return None

    def open_position_count(self) -> int:
        return len(self.get_open_positions())

    def has_capacity_for_new_position(self) -> bool:
        return self.open_position_count() < config.MAX_OPEN_POSITIONS

    # ------------------------------------------------------------------
    # Order fill confirmation
    # ------------------------------------------------------------------

    def _wait_for_fill(self, order_id, timeout: float = ORDER_FILL_TIMEOUT_SECONDS):
        """
        Polls an order until it reaches a filled (or partially-filled)
        state, or a terminal non-fill state (canceled/rejected/expired),
        or the timeout elapses. Returns the final Order object, or None
        if it never resolved within the timeout.

        This exists because every stop-placement and safety-net failure
        in production logs traced back to querying position/order state
        immediately after submit_order() returns -- before Alpaca had
        actually processed the fill. Market orders in paper trading are
        fast but not instant.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                order = self.client.get_order_by_id(order_id)
            except Exception as e:
                logger.warning("Polling order %s failed: %s", order_id, e)
                time.sleep(ORDER_FILL_POLL_INTERVAL_SECONDS)
                continue

            if order.status in FILLED_STATUSES:
                return order
            if order.status in (OrderStatus.CANCELED, OrderStatus.REJECTED,
                                 OrderStatus.EXPIRED, OrderStatus.SUSPENDED):
                logger.error("Order %s ended in non-fill state: %s", order_id, order.status)
                return order
            time.sleep(ORDER_FILL_POLL_INTERVAL_SECONDS)

        logger.error("Order %s did not resolve within %.1fs timeout", order_id, timeout)
        return None

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------

    def calc_position_notional(self, macro_sentiment: str, direction: str) -> float:
        """
        Returns the dollar amount to deploy for a new position, based on
        directional sizing rules and available cash, capped by
        MAX_PORTFOLIO_RISK_PCT.
        """
        account = self.get_account()
        cash = float(account.cash)
        pct = config.POSITION_SIZE_PCT.get(macro_sentiment, {}).get(direction, 0.0)
        max_deployable = cash * config.MAX_PORTFOLIO_RISK_PCT
        return min(cash * pct, max_deployable)

    # ------------------------------------------------------------------
    # Entry orders
    # ------------------------------------------------------------------

    def enter_long(self, symbol: str, notional: float, trail_percent: float) -> TradeResult:
        """Long entry: market order using notional value (fractional shares OK)."""
        if notional <= 0:
            return TradeResult(False, False, symbol, "long", error="Notional <= 0, skipped")
        try:
            order = self.client.submit_order(MarketOrderRequest(
                symbol=symbol,
                notional=round(notional, 2),
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            ))
            logger.info("LONG entry submitted %s notional=$%.2f order_id=%s", symbol, notional, order.id)
        except Exception as e:
            logger.error("Long entry failed for %s: %s", symbol, e)
            return TradeResult(False, False, symbol, "long", error=str(e))

        filled = self._wait_for_fill(order.id)
        if filled is None or filled.status not in FILLED_STATUSES:
            logger.error("%s: entry order did not fill (status=%s) -- no position, nothing to protect",
                         symbol, filled.status if filled else "unknown/timeout")
            return TradeResult(False, False, symbol, "long", order_id=str(order.id),
                               error="Entry order did not fill")

        fill_qty = math.floor(abs(float(filled.filled_qty))) if filled.filled_qty else None
        stop_result = self._place_trailing_stop_with_retry(symbol, OrderSide.SELL, trail_percent, qty=fill_qty)
        protected = stop_result is not None
        if not protected:
            logger.error("%s: entry FILLED but stop-loss could NOT be placed after retries -- "
                         "closing position via safety net", symbol)
            self._safety_net_close(symbol)

        return TradeResult(
            success=True, protected=protected, symbol=symbol, side="long",
            notional=notional, order_id=str(order.id),
            stop_order_id=stop_result,
            error=None if protected else "Stop-loss placement failed; position closed by safety net",
        )

    def enter_short(self, symbol: str, notional: float, last_price: float, trail_percent: float) -> TradeResult:
        """
        Short entry: market order using WHOLE share qty -- Alpaca rejects
        fractional shorts. If qty rounds down to zero, the trade is skipped.
        """
        if notional <= 0 or last_price <= 0:
            return TradeResult(False, False, symbol, "short", error="Notional or price <= 0, skipped")

        qty = math.floor(notional / last_price)
        if qty <= 0:
            logger.info("Short %s skipped: calculated qty rounded to 0", symbol)
            return TradeResult(False, False, symbol, "short", error="Qty rounded to zero, skipped")

        try:
            order = self.client.submit_order(MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            ))
            logger.info("SHORT entry submitted %s qty=%d order_id=%s", symbol, qty, order.id)
        except Exception as e:
            logger.error("Short entry failed for %s: %s", symbol, e)
            return TradeResult(False, False, symbol, "short", error=str(e))

        filled = self._wait_for_fill(order.id)
        if filled is None or filled.status not in FILLED_STATUSES:
            logger.error("%s: entry order did not fill (status=%s) -- no position, nothing to protect",
                         symbol, filled.status if filled else "unknown/timeout")
            return TradeResult(False, False, symbol, "short", order_id=str(order.id),
                               error="Entry order did not fill")

        fill_qty = math.floor(abs(float(filled.filled_qty))) if filled.filled_qty else qty
        stop_result = self._place_trailing_stop_with_retry(symbol, OrderSide.BUY, trail_percent, qty=fill_qty)
        protected = stop_result is not None
        if not protected:
            logger.error("%s: entry FILLED but stop-loss could NOT be placed after retries -- "
                         "closing position via safety net", symbol)
            self._safety_net_close(symbol)

        return TradeResult(
            success=True, protected=protected, symbol=symbol, side="short",
            qty=fill_qty, order_id=str(order.id),
            stop_order_id=stop_result,
            error=None if protected else "Stop-loss placement failed; position closed by safety net",
        )

    # ------------------------------------------------------------------
    # Exit / protection
    # ------------------------------------------------------------------

    def reconcile_orders(self) -> None:
        """Repair protection for positions and cancel orders with no position."""
        try:
            positions = {position.symbol: position for position in self.get_open_positions()}
            open_orders = self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
            orders_by_symbol = {}
            for order in open_orders:
                orders_by_symbol.setdefault(order.symbol, []).append(order)

            for symbol, orders in orders_by_symbol.items():
                if symbol not in positions:
                    for order in orders:
                        self.client.cancel_order_by_id(order.id)
                        logger.warning("Canceled orphan open order for %s: %s", symbol, order.id)

            for symbol, position in positions.items():
                position_qty = math.floor(abs(float(position.qty)))
                if position_qty <= 0:
                    logger.warning("%s has only fractional quantity; no trailing stop placed", symbol)
                    continue
                position_side = "long" if float(position.qty) > 0 else "short"
                expected_side = OrderSide.SELL if position_side == "long" else OrderSide.BUY
                protected = any(
                    order.type == OrderType.TRAILING_STOP and order.side == expected_side
                    for order in orders_by_symbol.get(symbol, [])
                )
                if not protected:
                    stop_id = self._place_trailing_stop_with_retry(
                        symbol, expected_side, config.DEFAULT_STOP_LOSS_PCT, qty=position_qty,
                    )
                    if stop_id:
                        logger.info("Startup protection restored for %s", symbol)
                    else:
                        logger.error("Startup protection could not be restored for %s", symbol)
        except Exception as e:
            logger.error("Startup order reconciliation failed: %s", e, exc_info=True)

    def cancel_open_orders(self, symbol: str) -> bool:
        """Cancel open orders for a symbol before closing or reversing it."""
        try:
            request = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
            orders = self.client.get_orders(request)
            for order in orders:
                self.client.cancel_order_by_id(order.id)
                logger.info("Canceled open order for %s: %s", symbol, order.id)

            deadline = time.monotonic() + ORDER_CANCEL_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                if not self.client.get_orders(request):
                    return True
                time.sleep(ORDER_FILL_POLL_INTERVAL_SECONDS)
            logger.error("Open orders for %s remained after cancellation timeout", symbol)
            return False
        except Exception as e:
            logger.error("Failed to cancel open orders for %s: %s", symbol, e)
            return False

    def _place_trailing_stop(self, symbol: str, exit_side: OrderSide,
                              trail_percent: float, qty: Optional[int]) -> Optional[str]:
        """
        Places a GTC trailing stop for a CONFIRMED-filled position. Trailing
        stops require whole shares. If qty wasn't passed in (shouldn't
        normally happen now that callers wait for fill first), falls back
        to a live position lookup.
        """
        try:
            if qty is None:
                position = self.get_position(symbol)
                if position is None:
                    logger.error("Cannot place trailing stop for %s: no position found", symbol)
                    return None
                qty = math.floor(abs(float(position.qty)))

            if qty <= 0:
                logger.error("Cannot place trailing stop for %s: whole-share qty is 0", symbol)
                return None

            stop_order = self.client.submit_order(TrailingStopOrderRequest(
                symbol=symbol,
                qty=qty,
                side=exit_side,
                type=OrderType.TRAILING_STOP,
                time_in_force=TimeInForce.GTC,
                trail_percent=round(trail_percent * 100, 2),  # config stores as fraction, API wants %
            ))
            logger.info("Trailing stop placed for %s: qty=%d trail=%.2f%% order_id=%s",
                        symbol, qty, trail_percent * 100, stop_order.id)
            return str(stop_order.id)
        except Exception as e:
            logger.error("Trailing stop attempt failed for %s: %s", symbol, e)
            return None

    def _place_trailing_stop_with_retry(self, symbol: str, exit_side: OrderSide,
                                         trail_percent: float, qty: Optional[int]) -> Optional[str]:
        """
        Retries stop placement a few times with a short delay. Even after
        confirming the entry order filled, Alpaca's paper engine can take
        a moment to settle the position/release the wash-trade lock on the
        opposite-side order -- this absorbs that lag instead of giving up
        on the first transient failure.
        """
        for attempt in range(1, STOP_PLACEMENT_MAX_ATTEMPTS + 1):
            stop_id = self._place_trailing_stop(symbol, exit_side, trail_percent, qty)
            if stop_id:
                return stop_id
            if attempt < STOP_PLACEMENT_MAX_ATTEMPTS:
                logger.warning("Stop placement for %s failed (attempt %d/%d), retrying in %.1fs",
                               symbol, attempt, STOP_PLACEMENT_MAX_ATTEMPTS, STOP_PLACEMENT_RETRY_DELAY_SECONDS)
                time.sleep(STOP_PLACEMENT_RETRY_DELAY_SECONDS)
        return None

    def _safety_net_close(self, symbol: str, max_attempts: int = 3):
        """
        If the trailing stop failed to submit after retries, close the
        position rather than hold it unprotected. Retries with a short
        delay for the same settlement-lag reason as stop placement --
        the original implementation gave up after one immediate attempt,
        which is why it failed 100% of the time in production.
        """
        logger.warning("SAFETY NET: closing unprotected position in %s", symbol)
        for attempt in range(1, max_attempts + 1):
            try:
                if not self.cancel_open_orders(symbol):
                    return
                self.client.close_position(symbol)
                logger.info("SAFETY NET: successfully closed unprotected position in %s", symbol)
                return
            except Exception as e:
                if attempt < max_attempts:
                    logger.warning("Safety-net close attempt %d/%d failed for %s: %s -- retrying",
                                   attempt, max_attempts, symbol, e)
                    time.sleep(STOP_PLACEMENT_RETRY_DELAY_SECONDS)
                else:
                    logger.error(
                        "SAFETY NET FAILED for %s after %d attempts: %s -- "
                        "POSITION MAY BE OPEN AND UNPROTECTED, MANUAL INTERVENTION NEEDED",
                        symbol, max_attempts, e,
                    )

    def close_position(self, symbol: str, wait_for_settle: bool = False) -> bool:
        """
        Closes a position. When wait_for_settle=True, blocks until the
        closing order fills (or times out) before returning -- use this
        before immediately opening an opposite-direction position on the
        same symbol (trend reversal), otherwise the new order can be
        rejected with "insufficient qty available" because the freed
        shares/buying power haven't settled yet.
        """
        if not self.cancel_open_orders(symbol):
            return False

        try:
            close_order = self.client.close_position(symbol)
            logger.info("Close submitted: %s", symbol)
        except Exception as e:
            logger.error("Failed to close position %s: %s", symbol, e)
            return False

        if wait_for_settle:
            order_id = getattr(close_order, "id", None)
            if order_id:
                filled = self._wait_for_fill(order_id)
                if filled is None or filled.status not in FILLED_STATUSES:
                    logger.warning("%s: close order did not confirm fill before timeout", symbol)
                    return False
        logger.info("Closed position: %s", symbol)
        return True

    def close_all_positions(self):
        try:
            self.client.close_all_positions(cancel_orders=True)
            logger.info("Closed all positions")
        except Exception as e:
            logger.error("Failed to close all positions: %s", e)
