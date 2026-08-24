"""
Central configuration for the trading bot.

All tunable parameters live here so trading_core.py, signal_engine.py,
and risk_agent.py stay clean. Nothing sensitive lives in this file --
API credentials are read from environment variables (see trading_core.py).
"""

import os

# ---------------------------------------------------------------------------
# Alpaca connection
# ---------------------------------------------------------------------------
# Credentials are NEVER hardcoded. Set these as environment variables:
#   export ALPACA_API_KEY="..."
#   export ALPACA_SECRET_KEY="..."
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
ALPACA_PAPER = True  # This bot is hardcoded to paper trading. Change deliberately.

# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------
WATCHLIST = [
    "MU", "NVDA", "QQQ", "RDDT", "JPM",
    "SNDK", "AMZN", "TSLA", "MSFT", "AAPL",
]

# ---------------------------------------------------------------------------
# Candle / polling settings
# ---------------------------------------------------------------------------
CANDLE_MINUTES = 3          # resample 1-minute yfinance bars to 3-minute candles
POLL_INTERVAL_SECONDS = 180  # how often the signal engine checks (3 minutes)
LOOKBACK_DAYS = 5           # how many days of 1-minute data to pull per check

# ---------------------------------------------------------------------------
# Indicator parameters
# ---------------------------------------------------------------------------
SUPERTREND_ATR_PERIOD = 10
SUPERTREND_MULTIPLIER = 3.0

EMA_FAST = 9
EMA_SLOW = 21

RSI_PERIOD = 14

ADX_PERIOD = 14
ADX_THRESHOLD = 15  # below this = choppy market, signal skipped

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

VOLUME_SURGE_MULTIPLE = 1.5  # for HIGH tier: volume > 1.5x 20-period average

# ---------------------------------------------------------------------------
# Dynamic RSI thresholds: [tier][macro_sentiment] -> (overbought, oversold)
# Macro sentiment defaults to NEUTRAL until a macro agent is wired in.
# ---------------------------------------------------------------------------
RSI_THRESHOLDS = {
    "HIGH":   {"BULLISH": (80, 30), "NEUTRAL": (75, 25), "BEARISH": (70, 20), "SIDELINE": (65, 15)},
    "MEDIUM": {"BULLISH": (75, 35), "NEUTRAL": (70, 30), "BEARISH": (65, 25), "SIDELINE": (60, 20)},
    "LOW":    {"BULLISH": (75, 35), "NEUTRAL": (70, 30), "BEARISH": (65, 25), "SIDELINE": (60, 20)},
}

# ---------------------------------------------------------------------------
# Signal rules
# ---------------------------------------------------------------------------
MIN_SIGNAL_STRENGTH = "LOW"   # minimum tier that passes through to the risk gate
DEDUP_WINDOW_MINUTES = 9      # won't re-fire same direction/ticker within this window

# ---------------------------------------------------------------------------
# Position sizing (directional, pre-risk-agent) as % of buying power
# ---------------------------------------------------------------------------
POSITION_SIZE_PCT = {
    "BULLISH":  {"long": 0.20, "short": 0.05},
    "NEUTRAL":  {"long": 0.10, "short": 0.10},
    "BEARISH":  {"long": 0.05, "short": 0.20},
    "SIDELINE": {"long": 0.0,  "short": 0.0},
}

# ---------------------------------------------------------------------------
# Risk / execution limits
# ---------------------------------------------------------------------------
MAX_OPEN_POSITIONS = 8
MAX_PORTFOLIO_RISK_PCT = 0.80   # max % of cash deployed across all positions
DEFAULT_STOP_LOSS_PCT = 0.02    # 2.0% trailing stop
MIN_WIN_PROBABILITY = 0.50      # rule-based risk gate threshold
MIN_TICKER_SCORE = 4.0          # out of 10, only used once a ticker-research agent exists

ENTRY_CUTOFF_HOUR_ET = 15
ENTRY_CUTOFF_MINUTE_ET = 30  # no new positions after 3:30 PM ET

MARKET_OPEN_HOUR_ET = 9
MARKET_OPEN_MINUTE_ET = 30
MARKET_CLOSE_HOUR_ET = 16
MARKET_CLOSE_MINUTE_ET = 0

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
STATE_DB_PATH = "trades.db"
