"""
main.py -- Bot V2 runner (local signal engine).

Polls the watchlist every CANDLE_MINUTES during market hours, routes
qualifying signals through the risk agent, and executes approved trades
via the shared TradeExecutor. Implements the entry rules and signal
routing logic from strategy guide sections 5-6:

  Entry rules:
    1. Signal must meet minimum strength (handled inside signal_engine)
    2. Signal must not be deduplicated (handled inside signal_engine)
    3. Risk agent must approve
    4. Max open positions not reached
    5. Before the entry cutoff time (exits always allowed)
    6. Sufficient capital available

  Signal routing:
    BULLISH signal -> close any short on that ticker (reversal), skip if
                       already long, else open long
    BEARISH signal -> close any long on that ticker (reversal), skip if
                       already short, else open short

NOT YET BUILT (see README): macro/ticker/prediction research agents,
VIX watchdog, ticker health checks, EOD position review, orchestrator
scheduling, post-mortem self-tuning, Bot V1 webhook listener. This
runner uses NEUTRAL macro sentiment and placeholder win-probability/
ticker-score values until those pieces exist.
"""

import logging
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import config
from trading_core import TradeExecutor, ConfigError
from signal_engine import SignalEngine
from risk_agent import RuleBasedRiskAgent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("main")

ET = ZoneInfo("America/New_York")

# Placeholder macro sentiment until a macro research agent exists.
# Change this manually, or wire in a real agent later.
MACRO_SENTIMENT = "NEUTRAL"


def get_server_time(executor: TradeExecutor) -> datetime:
    """Use Alpaca's clock so market decisions share the broker's time base."""
    try:
        return executor.get_clock().timestamp.astimezone(ET)
    except Exception as e:
        logger.warning("Alpaca clock unavailable; using local time: %s", e)
        return datetime.now(ET)


def is_market_open(now_et: datetime) -> bool:
    open_t = dtime(config.MARKET_OPEN_HOUR_ET, config.MARKET_OPEN_MINUTE_ET)
    close_t = dtime(config.MARKET_CLOSE_HOUR_ET, config.MARKET_CLOSE_MINUTE_ET)
    return now_et.weekday() < 5 and open_t <= now_et.time() <= close_t


def is_past_entry_cutoff(now_et: datetime) -> bool:
    cutoff = dtime(config.ENTRY_CUTOFF_HOUR_ET, config.ENTRY_CUTOFF_MINUTE_ET)
    return now_et.time() >= cutoff


def handle_signal(signal, executor: TradeExecutor, risk_agent: RuleBasedRiskAgent, now_et: datetime):
    symbol = signal.symbol
    with executor.symbol_lock(symbol):
        _handle_signal_locked(signal, executor, risk_agent, now_et)


def _handle_signal_locked(signal, executor: TradeExecutor, risk_agent: RuleBasedRiskAgent, now_et: datetime):
    symbol = signal.symbol
    position = executor.get_position(symbol)
    current_side = None
    if position is not None:
        current_side = "long" if float(position.qty) > 0 else "short"

    # --- Signal routing (reversal logic) ---
    if signal.direction == "BULLISH":
        if current_side == "short":
            logger.info("%s: closing short position (trend reversal)", symbol)
            decision = risk_agent.evaluate(symbol, signal.direction, MACRO_SENTIMENT, is_exit=True)
            if decision.approved:
                if not executor.close_position(symbol, wait_for_settle=True):
                    logger.error("%s: reversal close failed; skipping new entry", symbol)
                    return
            else:
                logger.warning("%s: reversal close rejected by risk agent; skipping new entry", symbol)
                return
        elif current_side == "long":
            logger.info("%s: already long, skipping duplicate signal", symbol)
            return
        target_side = "long"
    else:  # BEARISH
        if current_side == "long":
            logger.info("%s: closing long position (trend reversal)", symbol)
            decision = risk_agent.evaluate(symbol, signal.direction, MACRO_SENTIMENT, is_exit=True)
            if decision.approved:
                if not executor.close_position(symbol, wait_for_settle=True):
                    logger.error("%s: reversal close failed; skipping new entry", symbol)
                    return
            else:
                logger.warning("%s: reversal close rejected by risk agent; skipping new entry", symbol)
                return
        elif current_side == "short":
            logger.info("%s: already short, skipping duplicate signal", symbol)
            return
        target_side = "short"

    # --- Entry rule 5: cutoff time (exits above are always allowed, entries are not) ---
    if is_past_entry_cutoff(now_et):
        logger.info("%s: past entry cutoff (%s ET), no new positions", symbol, config.ENTRY_CUTOFF_HOUR_ET)
        return

    # --- Entry rule 4: max open positions ---
    if not executor.has_capacity_for_new_position():
        logger.info("%s: max open positions (%d) reached, skipping entry", symbol, config.MAX_OPEN_POSITIONS)
        return

    # --- Entry rule 3: risk agent approval ---
    # NOTE: win_probability and ticker_score are placeholders (0.55, 5.0)
    # until the Prediction Agent and Ticker Research Agent are built.
    decision = risk_agent.evaluate(
        symbol, signal.direction, MACRO_SENTIMENT,
        win_probability=0.55, ticker_score=5.0,
    )
    if not decision.approved:
        logger.info("%s: risk agent rejected -- %s", symbol, decision.reason)
        return

    # --- Entry rule 6: sufficient capital (calc_position_notional caps by cash) ---
    notional = executor.calc_position_notional(MACRO_SENTIMENT, target_side)
    if notional <= 0:
        logger.info("%s: no capital allocated for this direction/macro combo", symbol)
        return

    if target_side == "long":
        result = executor.enter_long(symbol, notional, decision.stop_loss_pct)
    else:
        result = executor.enter_short(symbol, notional, signal.price, decision.stop_loss_pct)

    if result.success:
        logger.info("%s: %s entry executed successfully", symbol, target_side)
    else:
        logger.error("%s: %s entry failed -- %s", symbol, target_side, result.error)


def run_once(executor: TradeExecutor, engine: SignalEngine, risk_agent: RuleBasedRiskAgent):
    now_et = get_server_time(executor)
    signals = engine.scan_watchlist(macro_sentiment=MACRO_SENTIMENT)
    for signal in signals:
        handle_signal(signal, executor, risk_agent, now_et)


def maybe_close_end_of_day(executor: TradeExecutor, now_et: datetime, closed_date):
    close_time = dtime(config.MARKET_CLOSE_HOUR_ET, config.MARKET_CLOSE_MINUTE_ET)
    minutes_to_close = (datetime.combine(now_et.date(), close_time, tzinfo=ET) - now_et).total_seconds() / 60
    if 0 <= minutes_to_close <= config.EOD_CLOSE_MINUTES_BEFORE and closed_date != now_et.date():
        logger.info("End-of-day shutdown: closing all positions and canceling orders")
        executor.close_all_positions()
        return now_et.date()
    return closed_date


def main():
    try:
        executor = TradeExecutor()
    except ConfigError as e:
        logger.error(str(e))
        return

    engine = SignalEngine()
    risk_agent = RuleBasedRiskAgent()
    executor.reconcile_orders()
    closed_date = None

    logger.info("Bot V2 starting. Watchlist: %s", config.WATCHLIST)

    while True:
        now_et = get_server_time(executor)
        closed_date = maybe_close_end_of_day(executor, now_et, closed_date)
        if is_market_open(now_et) and closed_date != now_et.date():
            try:
                run_once(executor, engine, risk_agent)
            except Exception as e:
                logger.error("Error during scan cycle: %s", e, exc_info=True)
        else:
            logger.info("Market closed (%s ET), sleeping...", now_et.strftime("%H:%M"))

        time.sleep(config.POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
