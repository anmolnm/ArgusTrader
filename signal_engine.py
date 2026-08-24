"""
signal_engine.py -- Bot V2's local signal engine.

Polls yfinance every N minutes during market hours, resamples 1-minute
bars to 3-minute candles, computes the 6-indicator stack, and classifies
each ticker into a signal tier (HIGH / MEDIUM / LOW / None) with a
direction (BULLISH / BEARISH). Applies dynamic RSI thresholds and
per-ticker/direction deduplication.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf

import config
import indicators

logger = logging.getLogger("signal_engine")


@dataclass
class Signal:
    symbol: str
    direction: str          # "BULLISH" or "BEARISH"
    tier: str                # "HIGH", "MEDIUM", "LOW"
    price: float
    timestamp: pd.Timestamp
    rsi: float
    adx: float
    reasons: list


class SignalEngine:
    def __init__(self):
        # dedup tracker: {(symbol, direction): last_fired_timestamp}
        self._last_fired = {}

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    def fetch_candles(self, symbol: str) -> Optional[pd.DataFrame]:
        """
        Pulls LOOKBACK_DAYS of 1-minute bars from yfinance and resamples
        to CANDLE_MINUTES candles. Returns None if data is unavailable
        or insufficient for indicator warmup.
        """
        try:
            raw = yf.download(
                symbol,
                period=f"{config.LOOKBACK_DAYS}d",
                interval="1m",
                progress=False,
                auto_adjust=True,
            )
        except Exception as e:
            logger.error("yfinance download failed for %s: %s", symbol, e)
            return None

        if raw is None or raw.empty:
            logger.warning("No data returned for %s", symbol)
            return None

        # yfinance sometimes returns MultiIndex columns for single-symbol downloads
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw = raw.rename(columns=str.lower)

        resampled = raw.resample(f"{config.CANDLE_MINUTES}min").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }).dropna()

        # Need enough bars for the slowest indicator (MACD slow=26) plus buffer
        min_bars = max(config.MACD_SLOW, config.EMA_SLOW, config.ADX_PERIOD) + 10
        if len(resampled) < min_bars:
            logger.warning("Insufficient candles for %s: %d < %d needed",
                            symbol, len(resampled), min_bars)
            return None

        return resampled

    # ------------------------------------------------------------------
    # Signal generation
    # ------------------------------------------------------------------

    def evaluate(self, symbol: str, macro_sentiment: str = "NEUTRAL") -> Optional[Signal]:
        """
        Fetches data, computes indicators, and returns a Signal if the
        current bar meets at least the LOW tier and isn't deduplicated.
        Returns None if no qualifying signal.
        """
        df = self.fetch_candles(symbol)
        if df is None:
            return None

        df = indicators.compute_all_indicators(df)
        latest = df.iloc[-1]

        if pd.isna(latest.get("adx")) or pd.isna(latest.get("rsi")):
            return None  # indicators not warmed up yet

        direction = None
        if latest["supertrend_direction"] == 1:
            direction = "BULLISH"
        elif latest["supertrend_direction"] == -1:
            direction = "BEARISH"
        if direction is None:
            return None

        tier = self._classify_tier(latest, direction)
        if tier is None:
            return None

        if not self._passes_rsi_filter(latest["rsi"], tier, macro_sentiment, direction):
            logger.info("%s %s signal blocked by RSI filter (rsi=%.1f)", symbol, direction, latest["rsi"])
            return None

        if self._is_deduplicated(symbol, direction, df.index[-1]):
            logger.info("%s %s signal deduplicated (fired recently)", symbol, direction)
            return None

        self._last_fired[(symbol, direction)] = df.index[-1]

        return Signal(
            symbol=symbol,
            direction=direction,
            tier=tier,
            price=float(latest["close"]),
            timestamp=df.index[-1],
            rsi=float(latest["rsi"]),
            adx=float(latest["adx"]),
            reasons=self._tier_reasons(latest, direction, tier),
        )

    def _classify_tier(self, latest: pd.Series, direction: str) -> Optional[str]:
        bull = direction == "BULLISH"

        supertrend_flip = bool(latest.get("ema_cross_bull" if bull else "ema_cross_bear", False))
        ema_aligned = bool(latest.get("ema_aligned_bull" if bull else "ema_aligned_bear", False))
        ema_cross = bool(latest.get("ema_cross_bull" if bull else "ema_cross_bear", False))
        vwap_aligned = bool(latest["above_vwap"]) if bull else not bool(latest["above_vwap"])
        adx_strong = bool(latest["adx_strong"])
        macd_confirms = bool(latest.get("macd_bull" if bull else "macd_bear", False))
        volume_surge = bool(latest.get("volume_surge", False))

        # HIGH: SuperTrend flip + EMA cross + VWAP aligned + ADX strong + MACD confirms + volume surge
        if ema_cross and vwap_aligned and adx_strong and macd_confirms and volume_surge:
            return "HIGH"

        # MEDIUM: SuperTrend flip + EMA aligned + VWAP aligned + ADX above threshold
        if ema_aligned and vwap_aligned and adx_strong:
            return "MEDIUM"

        # LOW: SuperTrend in direction (already true, we got here) + EMA cross + ADX above threshold
        if ema_cross and adx_strong:
            return "LOW"

        return None

    def _tier_reasons(self, latest: pd.Series, direction: str, tier: str) -> list:
        bull = direction == "BULLISH"
        reasons = [f"SuperTrend {'up' if bull else 'down'}trend"]
        if latest.get("ema_cross_bull" if bull else "ema_cross_bear"):
            reasons.append("EMA 9/21 crossover")
        elif latest.get("ema_aligned_bull" if bull else "ema_aligned_bear"):
            reasons.append("EMA 9/21 aligned")
        if (bull and latest["above_vwap"]) or (not bull and not latest["above_vwap"]):
            reasons.append("VWAP aligned")
        if latest["adx_strong"]:
            reasons.append(f"ADX strong ({latest['adx']:.1f})")
        if latest.get("macd_bull" if bull else "macd_bear"):
            reasons.append("MACD confirms")
        if latest.get("volume_surge"):
            reasons.append("Volume surge")
        return reasons

    def _passes_rsi_filter(self, rsi: float, tier: str, macro_sentiment: str, direction: str) -> bool:
        """A BULLISH (long) signal blocks if RSI >= overbought threshold.
        A BEARISH (short) signal blocks if RSI <= oversold threshold."""
        overbought, oversold = config.RSI_THRESHOLDS[tier][macro_sentiment]
        if direction == "BULLISH":
            return rsi < overbought
        else:
            return rsi > oversold

    def _is_deduplicated(self, symbol: str, direction: str, current_bar_time: pd.Timestamp) -> bool:
        last_time = self._last_fired.get((symbol, direction))
        if last_time is None:
            return False
        elapsed = (current_bar_time - last_time).total_seconds() / 60
        return elapsed < config.DEDUP_WINDOW_MINUTES

    def scan_watchlist(self, watchlist=None, macro_sentiment: str = "NEUTRAL") -> list:
        """Evaluates every ticker in the watchlist, returns a list of qualifying Signals."""
        watchlist = watchlist or config.WATCHLIST
        signals = []
        for symbol in watchlist:
            try:
                sig = self.evaluate(symbol, macro_sentiment)
                if sig:
                    signals.append(sig)
                    logger.info("SIGNAL: %s %s tier=%s price=%.2f reasons=%s",
                                sig.symbol, sig.direction, sig.tier, sig.price, sig.reasons)
            except Exception as e:
                logger.error("Error evaluating %s: %s", symbol, e)
        return signals
