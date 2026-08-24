# Strategy Guide

This document explains the trading logic behind this bot: what each
indicator measures, how they combine into a signal, and how a signal
becomes (or doesn't become) a trade. It's meant to stand alone -- you
shouldn't need to read the source to understand *why* the bot does what
it does, only *how*, if you want to change something.

> **This bot trades a paper (simulated) Alpaca account by default.**
> Nothing here is financial advice, and nothing about a strategy working
> in paper trading guarantees it will work with real money. Read
> [Limitations & Disclaimer](#limitations--disclaimer) before running
> this against a live account.

## Table of contents

- [Overview](#overview)
- [The indicators](#the-indicators)
  - [SuperTrend](#supertrend)
  - [EMA 9/21 crossover](#ema-921-crossover)
  - [RSI (Relative Strength Index)](#rsi-relative-strength-index)
  - [VWAP (Volume-Weighted Average Price)](#vwap-volume-weighted-average-price)
  - [ADX (Average Directional Index)](#adx-average-directional-index)
  - [MACD](#macd)
  - [Volume surge](#volume-surge)
- [How indicators become a signal](#how-indicators-become-a-signal)
  - [Signal strength tiers](#signal-strength-tiers)
  - [Dynamic RSI thresholds](#dynamic-rsi-thresholds)
  - [Deduplication](#deduplication)
- [How a signal becomes a trade](#how-a-signal-becomes-a-trade)
  - [The risk gate](#the-risk-gate)
  - [Position sizing](#position-sizing)
  - [Entry rules](#entry-rules)
  - [Signal routing (reversals)](#signal-routing-reversals)
- [Exit strategy](#exit-strategy)
- [What's not implemented yet](#whats-not-implemented-yet)
- [Limitations & Disclaimer](#limitations--disclaimer)

## Overview

The bot polls a watchlist of stocks every 3 minutes during market hours,
pulls recent price data, and computes six technical indicators on
3-minute candles. If enough of those indicators agree on a direction, it
counts as a "signal." Signals pass through a filter (RSI extremes,
duplicate suppression) and a risk gate (position sizing, macro
conditions) before an order is actually placed. Every position that
opens is meant to get an automatic trailing stop-loss immediately.

Nothing here predicts the future -- it's a rules-based reaction to
recent price/volume behavior, tuned toward catching the middle of a
move already in progress rather than picking tops or bottoms.

## The indicators

Each indicator looks at the same 3-minute OHLCV (open/high/low/close/
volume) candles from a different angle. No single one is trusted alone
-- the tier system below requires several to agree.

### SuperTrend

**What it measures:** which direction the trend is currently pointing,
using price volatility (via Average True Range) to set a band above/
below price that "flips" when the trend reverses.

**Parameters used:** ATR period 10, multiplier 3.0.

**How to read it:** SuperTrend outputs a single line plotted on the
price chart. When price is above the line, the trend is up (+1); when
price is below it, the trend is down (-1). The line only moves in the
direction that confirms the current trend, so it acts as a dynamic
trailing reference rather than a fixed level -- this is also why it's
a natural fit for trailing-stop-style exits, separate from its use here
as a directional filter.

**Why it's here:** it's the primary trend filter. The bot only
considers a BULLISH signal when SuperTrend is in an uptrend, and a
BEARISH signal when it's in a downtrend -- every other indicator either
confirms or refines that base direction, none of them override it.

### EMA 9/21 crossover

**What it measures:** short-term trend momentum, using two exponential
moving averages (EMAs) of closing price -- a fast one (9 periods) and a
slow one (21 periods). EMAs weight recent prices more heavily than a
simple moving average, so they react faster to new information.

**How to read it:** when the fast EMA crosses above the slow EMA, it
signals fresh bullish momentum ("golden cross" on this timeframe); when
it crosses below, fresh bearish momentum. Between crossovers, whichever
EMA is on top tells you the current alignment (still bullish/bearish)
even without a fresh cross.

**Why it's here:** distinguishes a *fresh* trend change (crossover,
higher conviction) from an *established* one (aligned but no recent
cross, lower conviction) -- this distinction directly drives the
HIGH/MEDIUM/LOW tier system below.

### RSI (Relative Strength Index)

**What it measures:** momentum, on a 0-100 scale, based on the size and
frequency of recent up-moves vs. down-moves over the last 14 periods.

**How to read it:** high RSI (traditionally >70) suggests a security
has moved up quickly and may be "overbought" -- more likely to pull
back or consolidate. Low RSI (<30) suggests "oversold" -- more likely to
bounce.

**Why it's here:** it's used as a *brake*, not a trigger. The bot never
enters a trade because of RSI alone -- it uses RSI to block trades that
would be chasing a move that's already stretched (buying something
already overbought, or shorting something already oversold). See
[Dynamic RSI thresholds](#dynamic-rsi-thresholds) for exactly how the
cutoff is calculated.

### VWAP (Volume-Weighted Average Price)

**What it measures:** the average price a security has traded at today,
weighted by volume at each price level. Unlike a simple moving average,
a price level with heavy volume pulls VWAP toward it more than a level
with light volume.

**How to read it:** VWAP resets at the start of each trading day. Price
above VWAP suggests buyers are in control on a volume-weighted basis
today; price below suggests sellers are. Institutional trading desks
commonly use VWAP as a benchmark for whether they got a "good" fill, so
it's often treated as a rough proxy for institutional positioning.

**Why it's here:** used as a same-day directional confirmation --
a BULLISH signal is considered stronger if price is also above VWAP,
and a BEARISH signal stronger if price is below it.

### ADX (Average Directional Index)

**What it measures:** trend *strength*, independent of direction, on a
0-100 scale, over a 14-period window.

**How to read it:** low ADX (below ~15-20) suggests a choppy, range-
bound market where directional indicators like SuperTrend and EMA
crossovers are prone to false signals. High ADX suggests a market
that's genuinely trending, where those same indicators are more
trustworthy.

**Why it's here:** it's a gate, not a direction signal. Below the
threshold (15 in this bot), the signal is skipped entirely regardless of
what the other indicators say -- the logic being that trend-following
indicators are least reliable exactly when there's no real trend to
follow.

### MACD

**What it measures:** momentum, via the relationship between two EMAs
(12-period and 26-period) and a signal line (9-period EMA of that
difference). The commonly-charted "histogram" is the gap between the
MACD line and its signal line.

**How to read it:** a positive/rising histogram suggests bullish
momentum is building; negative/falling suggests bearish momentum. It's
a lagging confirmation of a move already visible in the EMA crossover,
not an early-warning indicator.

**Why it's here:** an extra confirmation layer for the highest-conviction
tier (see below) -- it adds redundancy on top of the EMA crossover using
a different (slower, smoother) calculation, so a false EMA crossover
signal is less likely to also show MACD confirmation.

### Volume surge

**What it measures:** whether current volume is unusually high relative
to its recent 20-period average (this bot's threshold: >1.5x average).

**Why it's here:** a move on unusually high volume is more likely to be
driven by real conviction (news, institutional flow) than noise. It's
the final confirming factor required for the top signal tier only.

## How indicators become a signal

### Signal strength tiers

Every evaluation of a ticker produces one of: no signal, LOW, MEDIUM, or
HIGH. Direction (BULLISH/BEARISH) always comes from SuperTrend; the tier
reflects how much additional confirmation exists.

| Tier | Requirements |
|---|---|
| **HIGH** | EMA 9/21 fresh crossover **+** VWAP aligned **+** ADX above threshold **+** MACD confirms **+** volume surge (>1.5x average) |
| **MEDIUM** | EMA 9/21 aligned (cross or already aligned) **+** VWAP aligned **+** ADX above threshold |
| **LOW** | EMA 9/21 fresh crossover **+** ADX above threshold (continuation signal -- SuperTrend already established) |

Only signals at or above the configured minimum tier (`LOW` by default)
are passed on for further filtering. Every unmet condition below HIGH
that still meets MEDIUM/LOW isn't a "worse" version of the same signal
-- it's a genuinely different, lower-confirmation setup, so position
sizing and risk decisions downstream can (in principle) weight tiers
differently, even though the current rule-based risk gate treats them
the same.

### Dynamic RSI thresholds

The overbought/oversold RSI cutoff that can block a signal isn't fixed
-- it tightens or loosens based on signal tier and the day's macro
sentiment (currently hardcoded to NEUTRAL until a macro-sentiment
source is built -- see [What's not implemented yet](#whats-not-implemented-yet)).

| Tier | BULLISH macro | NEUTRAL macro | BEARISH macro | SIDELINE macro |
|---|---|---|---|---|
| **HIGH** | 80 / 30 | 75 / 25 | 70 / 20 | 65 / 15 |
| **MEDIUM** | 75 / 35 | 70 / 30 | 65 / 25 | 60 / 20 |
| **LOW** | 75 / 35 | 70 / 30 | 65 / 25 | 60 / 20 |

Format: overbought / oversold. A BULLISH (long) signal is blocked if
RSI is at or above the overbought number; a BEARISH (short) signal is
blocked if RSI is at or below the oversold number. The intent: in a
bullish macro backdrop, allow longs more room to run before calling them
overbought (higher cutoff), and be quicker to block shorts (higher
oversold cutoff too, since shorting into a bullish tape is fighting the
broader trend). The SIDELINE column is deliberately tight on both sides
-- if the macro backdrop is uncertain enough to sideline new entries,
signals need to be less extended to even be considered.

### Deduplication

Once a ticker fires a signal in a given direction, the same
ticker+direction won't fire again for 9 minutes (3 candles), even if it
continues to qualify every cycle. If the direction flips, it fires
immediately regardless of the dedup window -- reversals are never
suppressed.

## How a signal becomes a trade

### The risk gate

Every qualifying signal passes through a risk check before an order is
placed:

- **Exits always pass.** Closing an existing position for a trend
  reversal is never blocked by the risk gate.
- **Hard block:** macro sentiment is SIDELINE -- no new entries in
  either direction.
- **Otherwise**, approves if win probability and ticker score both meet
  minimum thresholds. In this phase, both of those inputs are
  placeholders (win probability 0.55, ticker score 5.0) rather than
  coming from a real prediction model -- see
  [What's not implemented yet](#whats-not-implemented-yet).

### Position sizing

Position size is a percentage of account cash, determined by whether
the trade direction agrees with or fights the macro backdrop:

| Macro sentiment | Long size | Short size |
|---|---|---|
| BULLISH | 20% | 5% |
| NEUTRAL | 10% | 10% |
| BEARISH | 5% | 20% |
| SIDELINE | 0% (blocked) | 0% (blocked) |

A trade aligned with the macro backdrop (long in a bullish tape, short
in a bearish one) gets the larger allocation; a trade against it gets
the smaller one. Total capital deployed across all open positions is
additionally capped at 80% of cash.

### Entry rules

All of the following must hold for a new position to open:

1. Signal meets the minimum strength tier
2. Signal isn't deduplicated
3. Risk gate approves
4. Fewer than the maximum open positions (8) are currently held
5. It's before the entry cutoff time (3:30 PM ET) -- exits are exempt
   from this cutoff
6. Sufficient capital is available for the calculated position size

### Signal routing (reversals)

- **BULLISH signal:** if a short position exists on that ticker, close
  it first (trend reversal), then evaluate a new long. If already long,
  skip -- no duplicate entry.
- **BEARISH signal:** mirror image -- close an existing long, evaluate a
  new short, skip if already short.

## Exit strategy

Every filled entry is meant to immediately receive a **GTC (good-till-
cancelled) trailing stop**, sized by the risk gate (2% by default). The
stop price trails the position's high-water mark and triggers a market
exit if price reverses by the trail percentage.

If the trailing stop can't be placed (a transient API/settlement issue),
the bot retries a few times before falling back to an immediate safety-
net close -- the position is never intentionally left open without
either a working stop or an immediate close, since holding a position
with no protection at all is treated as a worse outcome than exiting
early.

Positions also close early on a trend reversal signal in the opposite
direction, regardless of the trailing stop's status.

## What's not implemented yet

This repo currently implements the local signal engine, execution
layer, and a rule-based risk gate. The following pieces are referenced
above as placeholders or future work, and aren't built yet:

- A real macro-sentiment source (currently hardcoded to NEUTRAL)
- A ticker research/scoring model and a win-probability prediction model
  (currently hardcoded placeholder values)
- An AI-assisted risk agent (currently pure rule-based)
- VIX monitoring, ticker health checks, and end-of-day position review
- Trade history persistence and any self-tuning/learning based on past
  results
- A second signal source (e.g., a webhook-driven bot) alongside this
  local engine

Anyone extending this should treat those as the natural next steps, in
roughly that order -- risk/exit logic before signal-quality logic.

## Limitations & Disclaimer

- **This is not financial advice**, and nothing in this repo constitutes
  a recommendation to buy or sell any security.
- **Backtesting is not included.** Nothing here has been validated
  against historical data -- performance in paper trading over a few
  sessions is not a meaningful predictor of edge.
- **Default configuration trades a paper account.** Switching to a live
  account is a deliberate, separate decision -- review the risk logic
  independently before doing so, and understand that real capital is
  subject to slippage, fees, and execution risk this bot does not model.
- **No warranty.** This code is provided for educational purposes. Use
  it, modify it, and trade with it entirely at your own risk.
