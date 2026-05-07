#!/usr/bin/env python3
"""
momentum_chaser_bot.py — Momentum Chaser Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Scans stocks and crypto for assets with high volume
and significant price acceleration (momentum breakout).

Real market data. No simulation.

Requirements:
    pip install yfinance requests
"""

import time
import requests
import yfinance as yf

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "momentum_chaser_bot"
BOT_NAME = "Momentum Chaser Bot"

# ── Assets to scan ─────────────────────────────────────────────────────────────
# symbol → (volume_avg_days, price_short_days, price_long_days,
#           min realistic price, max realistic price)
# - volume_avg_days  : lookback to compute average daily volume
# - price_short_days : recent window for price acceleration (e.g. 5 days)
# - price_long_days  : longer baseline for returns comparison (e.g. 20 days)
# - min/max price    : sanity guard to skip wrong-exchange data
ASSETS = {
    "AAPL":    (50, 5, 20,   80,   600),
    "TSLA":    (30, 3, 15,  100,  1500),
    "NVDA":    (30, 3, 15,  200,  2500),
    "MSFT":    (50, 5, 20,  200,   700),
    "SPY":     (50, 5, 20,  300,   700),
    "QQQ":     (50, 5, 20,  250,   700),
    "BTC-USD": (20, 3, 10, 8000, 250000),
    "ETH-USD": (20, 3, 10,  800,  20000),
}

# ── Momentum thresholds ────────────────────────────────────────────────────────
# Volume surge: current daily volume > average * FACTOR triggers an alert
VOLUME_SURGE_FACTOR = 1.5

# Price acceleration: short-term return (short_days) minus long-term return (long_days)
# positive means price is speeding up vs. its recent trend
MOMENTUM_THRESHOLDS = {
    "error":   15,   # extreme acceleration — possible breakout
    "warning": 8,    # strong acceleration — worth watching
    "info":    3,    # mild acceleration — early signal
}

SCAN_INTERVAL      = 60   # seconds between full scans
HEARTBEAT_INTERVAL = 20   # seconds between heartbeats to the hub

_last_hb = 0.0


# ── Hub helpers ────────────────────────────────────────────────────────────────

def _post(summary: str, level: str = "info", payload: dict = None) -> None:
    try:
        requests.post(f"{HUB}/ingest", json={
            "bot_id":   BOT_ID,
            "bot_name": BOT_NAME,
            "summary":  summary,
            "level":    level,
            "payload":  payload or {},
        }, timeout=5)
    except Exception:
        pass   # hub not yet ready or temporarily down


def _heartbeat() -> None:
    global _last_hb
    if time.time() - _last_hb < HEARTBEAT_INTERVAL:
        return
    try:
        requests.post(f"{HUB}/heartbeat/{BOT_ID}", json={
            "bot_name": BOT_NAME,
            "status":   "online",
        }, timeout=3)
    except Exception:
        pass
    _last_hb = time.time()


def _wait_for_hub() -> None:
    """Block until the BotController hub responds, up to 60 seconds."""
    for _ in range(60):
        try:
            if requests.get(HUB, timeout=2).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1)


# ── Market data ────────────────────────────────────────────────────────────────

def _fetch(symbol: str, ma_vol_days: int, short_days: int, long_days: int):
    """
    Returns (
        live_price,
        avg_volume,
        latest_volume,
        short_return,  # % return over short_days
        long_return    # % return over long_days
    ) or None on failure.
    """
    try:
        ticker = yf.Ticker(symbol)

        # ── Live price ──────────────────────────────────────────────────────
        fi    = ticker.fast_info
        price = getattr(fi, "last_price", None)
        if not price or price <= 0:
            df = ticker.history(period="2d", interval="1d")
            if df.empty:
                return None
            price = float(df["Close"].iloc[-1])

        # ── Volume ──────────────────────────────────────────────────────────
        # Get enough history to compute both volume average and returns
        max_days = max(ma_vol_days, long_days) + 10
        hist = ticker.history(period=f"{max_days}d", interval="1d")
        if hist.empty or len(hist) < long_days + 1:
            return None

        # Average volume over the last `ma_vol_days` (excluding today if it's incomplete)
        vol_series = hist["Volume"].tail(ma_vol_days + 1).iloc[:-1]  # yesterday and older
        avg_vol = float(vol_series.mean()) if len(vol_series) > 0 else 0.0

        # Latest completed day's volume (yesterday)
        latest_vol = float(hist["Volume"].iloc[-2])  # yesterday

        # ── Price returns ──────────────────────────────────────────────────
        closes = hist["Close"].tolist()
        # short return: from long_days ago to short_days ago? No, we want recent acceleration.
        # Approach: compute the return over the most recent `short_days` and over the `long_days` window.
        # Typically momentum = short-term return exceeding long-term trend.
        # We'll compute returns as percentage change from the start of each window to the latest close.
        # We'll use "yesterday" as the last full day to avoid today's partial data noise.
        # So reference price is yesterday's close = closes[-2] (since closes[-1] is today's maybe incomplete)
        yesterday = closes[-2]
        # short-term return: change from short_days+1 days ago to yesterday
        if len(closes) >= short_days + 2:
            short_start = closes[-(short_days + 2)]  # yesterday minus short_days days
            short_ret = (yesterday - short_start) / short_start * 100
        else:
            return None

        # long-term return: change from long_days+1 days ago to yesterday
        if len(closes) >= long_days + 2:
            long_start = closes[-(long_days + 2)]
            long_ret = (yesterday - long_start) / long_start * 100
        else:
            return None

        return price, avg_vol, latest_vol, short_ret, long_ret

    except Exception:
        return None


# ── Scan logic ─────────────────────────────────────────────────────────────────

def _scan() -> None:
    for symbol, (vol_days, short_d, long_d, price_min, price_max) in ASSETS.items():
        result = _fetch(symbol, vol_days, short_d, long_d)
        if result is None:
            _post(
                f"{symbol}: could not fetch market data — will retry next scan",
                "warning",
                {"symbol": symbol},
            )
            _heartbeat()
            continue

        price, avg_vol, latest_vol, short_ret, long_ret = result

        # ── Sanity check ──────────────────────────────────────────────────
        if not (price_min <= price <= price_max):
            _post(
                f"{symbol}: fetched price {price:.2f} outside expected range "
                f"{price_min}–{price_max} — skipping to avoid false signal",
                "warning",
                {"symbol": symbol, "fetched_price": price,
                 "expected_range": f"{price_min}–{price_max}"},
            )
            _heartbeat()
            continue

        # ── Volume surge ──────────────────────────────────────────────────
        volume_surge = False
        if avg_vol > 0 and latest_vol > avg_vol * VOLUME_SURGE_FACTOR:
            volume_surge = True

        # ── Price acceleration ────────────────────────────────────────────
        acceleration = short_ret - long_ret   # positive means moving faster

        # Determine alert level (only if acceleration exceeds a threshold AND there's volume)
        level = None
        if volume_surge and acceleration > 0:
            for lvl, threshold in sorted(MOMENTUM_THRESHOLDS.items(), key=lambda x: -x[1]):
                if acceleration >= threshold:
                    level = lvl
                    break

        payload = {
            "symbol":       symbol,
            "price":        round(price, 2),
            "volume_avg":   round(avg_vol, 2),
            "volume_latest": round(latest_vol, 2),
            "volume_surge": volume_surge,
            "short_return": round(short_ret, 2),
            "long_return":  round(long_ret, 2),
            "acceleration": round(acceleration, 2),
        }

        if level:
            direction = "up" if acceleration > 0 else "down"
            _post(
                f"{symbol}  +{acceleration:.1f}% acceleration vs baseline  "
                f"(vol {latest_vol:.0f} vs avg {avg_vol:.0f})  — momentum breakout candidate",
                level,
                payload,
            )
        else:
            # Quiet status pulse
            if acceleration > 0:
                summary = (f"{symbol}  +{acceleration:.1f}% acceleration  "
                           f"(price {price:.2f})  — no volume surge"
                           if not volume_surge else "")
            else:
                summary = (f"{symbol}  {acceleration:.1f}% accel  "
                           f"(price {price:.2f})  — no momentum")
            _post(summary or f"{symbol}  checking...", "info", payload)

        _heartbeat()
        time.sleep(1.5)


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    _wait_for_hub()

    _post(
        "Momentum Chaser Bot online — scanning for volume-backed price acceleration.",
        "info",
        {"assets": list(ASSETS.keys()),
         "volume_surge_factor": VOLUME_SURGE_FACTOR,
         "momentum_thresholds_pct": MOMENTUM_THRESHOLDS},
    )

    while True:
        _scan()
        _heartbeat()
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
