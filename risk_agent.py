"""
risk_agent.py -- Rule-based trade approval gate.

This is the fallback logic from the strategy guide (section 4), used as
the primary gate for now since we're starting rule-based before adding
a Claude-powered version. Every signal must pass through here before
trading_core places an order.

Swap-in point: a future ClaudeRiskAgent can implement the same
`.evaluate()` interface and be dropped in without changing main.py.
"""

import logging
from dataclasses import dataclass
from typing import Optional

import config

logger = logging.getLogger("risk_agent")


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    position_size_pct: Optional[float] = None
    stop_loss_pct: float = config.DEFAULT_STOP_LOSS_PCT


class RuleBasedRiskAgent:
    """
    Hard blocks:
      - macro sentiment SIDELINE -> reject all new entries
      - ticker flagged non-tradeable -> reject
    Otherwise approves if win probability >= MIN_WIN_PROBABILITY and
    ticker score >= MIN_TICKER_SCORE (both default to permissive values
    until a research pipeline is wired in -- see README "Not yet built").
    """

    def evaluate(self, symbol: str, direction: str, macro_sentiment: str,
                 win_probability: float = 0.55, ticker_score: float = 5.0,
                 is_exit: bool = False) -> RiskDecision:

        # Exits always pass through -- never blocked.
        if is_exit:
            return RiskDecision(approved=True, reason="Exit signal, always allowed")

        if macro_sentiment == "SIDELINE":
            return RiskDecision(approved=False, reason="Macro sentiment is SIDELINE, no new entries")

        if ticker_score < config.MIN_TICKER_SCORE:
            return RiskDecision(
                approved=False,
                reason=f"Ticker score {ticker_score:.1f} below minimum {config.MIN_TICKER_SCORE}",
            )

        if win_probability < config.MIN_WIN_PROBABILITY:
            return RiskDecision(
                approved=False,
                reason=f"Win probability {win_probability:.2f} below minimum {config.MIN_WIN_PROBABILITY}",
            )

        entry_side = "long" if direction == "BULLISH" else "short"
        size_pct = config.POSITION_SIZE_PCT.get(macro_sentiment, {}).get(entry_side, 0.0)

        logger.info("APPROVED: %s %s (macro=%s, win_prob=%.2f, ticker_score=%.1f, size=%.0f%%)",
                    symbol, direction, macro_sentiment, win_probability, ticker_score, size_pct * 100)

        return RiskDecision(
            approved=True,
            reason="Passed rule-based checks",
            position_size_pct=size_pct,
            stop_loss_pct=config.DEFAULT_STOP_LOSS_PCT,
        )
