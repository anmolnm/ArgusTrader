# Changelog

## Unreleased -- fill-race-condition fix (from Aug 21 & 24 log analysis)

### Added after Aug 25-26 log review
- **`trading_core.py`**: cancels symbol-scoped open orders before exits and
  waits for Alpaca to release reserved shares, eliminating trailing-stop
  reservation conflicts during reversals.
- **`trading_core.py`**: reconciles positions and open orders at startup,
  cancels orphan orders, and restores missing whole-share trailing stops.
- **`trading_core.py`**: serializes operations per symbol so concurrent signal
  sources cannot issue conflicting orders for the same ticker.
- **`main.py`**: uses Alpaca's server clock, performs one end-of-day
  liquidation window, and skips signal scans after liquidation.
- **`tests/test_trading_core.py`**: adds mock coverage for cancellation,
  cancellation timeouts, and startup protection repair.

### Fixed
- **`trading_core.py`**: `enter_long` / `enter_short` now wait for the
  entry order to reach a confirmed fill status (`_wait_for_fill`,
  polling every 0.3s up to 10s) before doing anything else. Previously
  they proceeded immediately after `submit_order()` returned, which is
  only an acknowledgment that the order was *accepted*, not filled.
- **`trading_core.py`**: trailing-stop placement now uses the actual
  `filled_qty` from the confirmed order instead of a separate position
  lookup, and retries up to 3 times with a 1s delay
  (`_place_trailing_stop_with_retry`) to absorb any remaining
  settlement lag or transient wash-trade conflicts.
- **`trading_core.py`**: the safety-net close (`_safety_net_close`) now
  retries up to 3 times instead of making one attempt and giving up --
  in production logs it failed 100% of the time (33/33, 31/31) because
  it hit the exact same unsettled-order race as the stop placement.
- **`trading_core.py`**: `close_position()` takes a new
  `wait_for_settle` flag. `main.py`'s reversal logic (close the opposite
  side, then immediately open a new position on the same symbol) now
  passes `wait_for_settle=True`, fixing the `insufficient qty available`
  errors seen when a reversal fired before the prior close had cleared.
- **`main.py`**: log output on entry now distinguishes three outcomes
  instead of two -- filled & protected (info), filled but unprotected
  (error, previously silently logged as success), and failed to fill
  (error). This was the most important fix: the old code reported
  "entry executed successfully" for all 64 trades across both sessions,
  even though 0 of them ever got a working stop-loss.

### Added
- `TradeResult.protected: bool` -- separates "did the entry fill" from
  "is this position actually covered by a stop." Both fields are now
  necessary to call a trade healthy.
- `TradeExecutor._wait_for_fill()` -- shared polling helper used by both
  entry functions and (optionally) `close_position`.

### Verified
- Mock-client test reproducing the exact lag pattern from the logs
  (order takes 3 polls to show as filled): confirms the entry now waits
  it out and the stop places successfully on the first real attempt.
- Mock-client test where stop placement genuinely fails every attempt:
  confirms `protected=False` is honestly reported and the safety net
  retries past a transient failure instead of giving up immediately.
- Live end-to-end verification against the real Alpaca paper API still
  needs to happen on your machine -- this environment can't reach
  Alpaca's or Yahoo Finance's servers, so testing here was necessarily
  mock-based.
