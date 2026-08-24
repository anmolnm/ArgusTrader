"""
indicators.py -- Technical indicator calculations for the local signal engine.

Takes OHLCV DataFrames (columns: open, high, low, close, volume) and
returns the same DataFrame with indicator columns appended.

SuperTrend isn't in the `ta` library, so it's implemented directly.
Everything else uses `ta`.
"""

import numpy as np
import pandas as pd
import ta

import config


def add_supertrend(df: pd.DataFrame, atr_period: int = config.SUPERTREND_ATR_PERIOD,
                    multiplier: float = config.SUPERTREND_MULTIPLIER) -> pd.DataFrame:
    """
    Adds 'supertrend' (the line value) and 'supertrend_direction'
    (+1 = uptrend, -1 = downtrend) columns.
    """
    high, low, close = df["high"], df["low"], df["close"]

    atr = ta.volatility.average_true_range(high, low, close, window=atr_period)
    hl2 = (high + low) / 2

    upperband = hl2 + multiplier * atr
    lowerband = hl2 - multiplier * atr

    final_upperband = upperband.copy()
    final_lowerband = lowerband.copy()
    supertrend = pd.Series(index=df.index, dtype=float)
    direction = pd.Series(index=df.index, dtype=int)

    for i in range(len(df)):
        if i == 0:
            supertrend.iloc[i] = final_upperband.iloc[i]
            direction.iloc[i] = 1
            continue

        # Band adjustment: bands can only move in the trend-confirming direction
        if close.iloc[i - 1] > final_upperband.iloc[i - 1]:
            final_upperband.iloc[i] = max(upperband.iloc[i], final_upperband.iloc[i - 1]) \
                if upperband.iloc[i] < final_upperband.iloc[i - 1] else upperband.iloc[i]
        if close.iloc[i - 1] <= final_upperband.iloc[i - 1]:
            final_upperband.iloc[i] = min(upperband.iloc[i], final_upperband.iloc[i - 1])

        if close.iloc[i - 1] < final_lowerband.iloc[i - 1]:
            final_lowerband.iloc[i] = min(lowerband.iloc[i], final_lowerband.iloc[i - 1]) \
                if lowerband.iloc[i] > final_lowerband.iloc[i - 1] else lowerband.iloc[i]
        if close.iloc[i - 1] >= final_lowerband.iloc[i - 1]:
            final_lowerband.iloc[i] = max(lowerband.iloc[i], final_lowerband.iloc[i - 1])

        prev_st = supertrend.iloc[i - 1]
        if prev_st == final_upperband.iloc[i - 1] and close.iloc[i] <= final_upperband.iloc[i]:
            supertrend.iloc[i] = final_upperband.iloc[i]
            direction.iloc[i] = -1
        elif prev_st == final_upperband.iloc[i - 1] and close.iloc[i] > final_upperband.iloc[i]:
            supertrend.iloc[i] = final_lowerband.iloc[i]
            direction.iloc[i] = 1
        elif prev_st == final_lowerband.iloc[i - 1] and close.iloc[i] >= final_lowerband.iloc[i]:
            supertrend.iloc[i] = final_lowerband.iloc[i]
            direction.iloc[i] = 1
        elif prev_st == final_lowerband.iloc[i - 1] and close.iloc[i] < final_lowerband.iloc[i]:
            supertrend.iloc[i] = final_upperband.iloc[i]
            direction.iloc[i] = -1
        else:
            supertrend.iloc[i] = final_upperband.iloc[i]
            direction.iloc[i] = 1

    df = df.copy()
    df["supertrend"] = supertrend
    df["supertrend_direction"] = direction
    return df


def add_ema(df: pd.DataFrame, fast: int = config.EMA_FAST, slow: int = config.EMA_SLOW) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = ta.trend.ema_indicator(df["close"], window=fast)
    df["ema_slow"] = ta.trend.ema_indicator(df["close"], window=slow)
    df["ema_aligned_bull"] = df["ema_fast"] > df["ema_slow"]
    df["ema_aligned_bear"] = df["ema_fast"] < df["ema_slow"]
    # crossover: alignment flipped vs previous bar
    df["ema_cross_bull"] = df["ema_aligned_bull"] & ~df["ema_aligned_bull"].shift(1).fillna(False)
    df["ema_cross_bear"] = df["ema_aligned_bear"] & ~df["ema_aligned_bear"].shift(1).fillna(False)
    return df


def add_rsi(df: pd.DataFrame, period: int = config.RSI_PERIOD) -> pd.DataFrame:
    df = df.copy()
    df["rsi"] = ta.momentum.rsi(df["close"], window=period)
    return df


def add_vwap(df: pd.DataFrame) -> pd.DataFrame:
    """
    VWAP resets daily at market open. Assumes df.index is tz-aware datetime
    and may span multiple days -- groups by date.
    """
    df = df.copy()
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    tp_vol = typical_price * df["volume"]
    date_group = df.index.date
    df["vwap"] = tp_vol.groupby(date_group).cumsum() / df["volume"].groupby(date_group).cumsum()
    df["above_vwap"] = df["close"] > df["vwap"]
    return df


def add_adx(df: pd.DataFrame, period: int = config.ADX_PERIOD) -> pd.DataFrame:
    df = df.copy()
    df["adx"] = ta.trend.adx(df["high"], df["low"], df["close"], window=period)
    df["adx_strong"] = df["adx"] > config.ADX_THRESHOLD
    return df


def add_macd(df: pd.DataFrame, fast: int = config.MACD_FAST, slow: int = config.MACD_SLOW,
             signal: int = config.MACD_SIGNAL) -> pd.DataFrame:
    df = df.copy()
    macd_obj = ta.trend.MACD(df["close"], window_fast=fast, window_slow=slow, window_sign=signal)
    df["macd"] = macd_obj.macd()
    df["macd_signal"] = macd_obj.macd_signal()
    df["macd_hist"] = macd_obj.macd_diff()
    df["macd_bull"] = df["macd_hist"] > 0
    df["macd_bear"] = df["macd_hist"] < 0
    return df


def add_volume_surge(df: pd.DataFrame, window: int = 20,
                      multiple: float = config.VOLUME_SURGE_MULTIPLE) -> pd.DataFrame:
    df = df.copy()
    avg_vol = df["volume"].rolling(window).mean()
    df["volume_surge"] = df["volume"] > (avg_vol * multiple)
    return df


def compute_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Runs the full 6-indicator stack on an OHLCV DataFrame."""
    df = add_supertrend(df)
    df = add_ema(df)
    df = add_rsi(df)
    df = add_vwap(df)
    df = add_adx(df)
    df = add_macd(df)
    df = add_volume_surge(df)
    return df
