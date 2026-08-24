# Trading Bot -- Phase 1 (Core Executor + Local Signal Engine)

## ⚠️ Post-mortem: Aug 21 & 24 sessions (fixed)

Analysis of both sessions' logs found that **every single trade placed
(64 out of 64) ran with zero stop-loss protection**, and the bot logged
"executed successfully" anyway. Root cause: `trading_core.py` checked
for the resulting position/attempted the trailing stop *immediately*
after submitting the entry order -- often within ~75ms -- before
Alpaca's paper engine had actually filled it. That caused three
cascading failures every time: the trailing stop failed ("no position
found" or a wash-trade conflict), the safety-net close meant to catch
that also failed (same reason), and the result was still reported as a
success because `TradeResult` only tracked "did the order submit," not
"is this position protected."

**Fixed** in this version: entries now wait for order-fill confirmation
before touching the position, stop placement retries a few times to
absorb settlement lag, the safety net retries instead of giving up
after one attempt, reversal closes wait to settle before re-entering,
and `TradeResult` now has a `protected` field so a filled-but-unprotected
position is logged as an error, not a success. See CHANGELOG.md for the
full diff summary. Both fixes are covered by mock-based tests since this
sandbox can't reach the live Alpaca API.

**Recommendation:** don't treat any P&L from the Aug 21/24 sessions as
representative of the strategy's edge -- those trades weren't running
the risk profile your guide specifies (2% trailing stop), they were
running with no stop at all.


This is the foundation of the multi-agent system described in the
original strategy guide: **Bot V2**, the local 6-indicator engine,
running against an Alpaca **paper** account with a rule-based risk gate.
Bot V1 (TradingView webhooks), the Claude-powered agents, the
orchestrator, and self-tuning post-mortem loop are not built yet -- see
"What's not built" below.

📖 **For a full explanation of the indicators and strategy logic (what
each one measures and why it's used), see [STRATEGY.md](STRATEGY.md).**

Licensed under [MIT](LICENSE).

## 1. Setup

### Install dependencies
```bash
pip install -r requirements.txt --break-system-packages
```
(Drop `--break-system-packages` if you're using a virtualenv, which is
recommended for a long-running bot.)

### Set your Alpaca paper credentials as environment variables
Never put these in a file that gets committed to git. Add to your shell
profile (`~/.zshrc` or `~/.bashrc`):
```bash
export ALPACA_API_KEY="your_paper_key"
export ALPACA_SECRET_KEY="your_paper_secret"
```
Then `source ~/.zshrc` (or open a new terminal). The bot reads these at
startup and refuses to run if they're missing -- it will never accept
keys as a script argument or hardcoded literal.

Since you mentioned an earlier key pair was pasted into a chat, make
sure you're using a **regenerated** key pair from the Alpaca dashboard,
not the original.

### Run it
```bash
python3 main.py
```
It checks the market clock every 3 minutes (matching your 3-minute
candle setup), and only trades during regular market hours (9:30 AM -
4:00 PM ET, weekdays).

## 2. What this phase includes

- **`config.py`** -- every tunable parameter from your guide (watchlist,
  indicator settings, RSI threshold matrix, position sizing table, risk
  limits, schedule times) in one place.
- **`trading_core.py`** -- the shared `TradeExecutor`: Alpaca connection,
  position sizing from cash and directional %, long entries (notional/
  fractional), short entries (whole-share, skips if qty rounds to 0),
  GTC trailing stops placed immediately after entry, and the
  "unprotected position" safety net that closes a position if its
  trailing stop fails to submit.
- **`indicators.py`** -- SuperTrend, EMA 9/21, RSI, VWAP (resets daily),
  ADX, MACD, and volume-surge detection, all matching the parameters in
  your guide.
- **`signal_engine.py`** -- pulls 1-minute bars from yfinance, resamples
  to 3-minute candles, computes the full indicator stack, classifies
  each ticker into HIGH/MEDIUM/LOW tiers (or no signal) using your exact
  tier rules, applies the dynamic RSI threshold matrix, and dedups
  same-direction signals within a 9-minute window.
- **`risk_agent.py`** -- the rule-based fallback logic from section 4 of
  your guide: SIDELINE macro blocks all entries, exits always pass,
  otherwise approves based on win-probability and ticker-score
  thresholds. This is a placeholder for the Claude Risk Agent -- same
  `.evaluate()` interface, so it's a drop-in swap later.
- **`main.py`** -- the polling loop: scans the watchlist, applies the
  signal routing logic (reversal closes, skip-if-already-positioned,
  new entry), and enforces entry rules 3-6 (risk approval, max open
  positions, entry cutoff at 3:30 PM ET, capital availability).

## 3. What's NOT built yet (from your guide, later phases)

- Bot V1 (TradingView webhook listener + ngrok tunnel)
- Macro Research Agent, Ticker Research Agent, Prediction Agent (Claude
  Opus-powered) -- `main.py` currently hardcodes `MACRO_SENTIMENT =
  "NEUTRAL"` and passes placeholder `win_probability=0.55`,
  `ticker_score=5.0` into the risk agent
- Claude-powered Risk Agent (currently rule-based only, per your choice)
- VIX watchdog, hourly ticker health checks, negative-headline scanning
- End-of-day position review (3:55 PM close-if-unprotected-or-losing)
- Orchestrator with the full daily schedule
- SQLite trade history (`trades.db`) and post-mortem self-tuning
- XGBoost prediction model transition at 50+ trades
- launchd startup script, iMessage failure alerts, dashboard

## 4. Suggested next steps

1. Run it during market hours and watch the logs -- confirm signals are
   firing sensibly on your actual watchlist before trusting it further.
2. Add SQLite persistence (`agents/state.py` equivalent) so trades are
   recorded -- needed before the post-mortem/self-tuning phase makes
   sense.
3. Build the Macro Research Agent next, since `main.py` currently
   hardcodes NEUTRAL sentiment, which understates the position-sizing
   logic your guide describes.
4. Add the orchestrator/scheduler once you have more than one
   scheduled research task to coordinate.

## 5. Safety notes

- This is hardcoded to `paper=True` in `config.py`. Flipping that to
  live trading is a deliberate, separate decision you'd need to make
  explicitly -- don't do it without independently re-reviewing the risk
  logic.
- The dedup and RSI-filter logic runs in-memory only right now (the
  `SignalEngine` instance's `_last_fired` dict). If you restart the
  bot mid-day, dedup state resets.
