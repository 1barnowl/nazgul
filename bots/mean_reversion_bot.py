#!/usr/bin/env python3
"""
mean_reversion_bot.py — BNF-style Mean Reversion Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Scans stocks and crypto for assets trading significantly
below their moving average (bottom-fishing signal).

Real market data. No simulation.

Requirements:
    pip install yfinance requests
"""

import time
import requests
import yfinance as yf

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "mean_reversion_bot"
BOT_NAME = "Mean Reversion Bot"

# ── Assets to scan ─────────────────────────────────────────────────────────────
# symbol -> (MA period in days, min realistic price, max realistic price)
# The min/max range is a sanity guard — if the fetched price falls outside
# this band it means yfinance returned data from the wrong exchange or
# a stale cache, and we skip rather than send a false signal.
ASSETS = {
    "AAPL":    (50,   80,   600),
    "TSLA":    (30,  100,  1500),
    "NVDA":    (30,  200,  2500),
    "MSFT":    (50,  200,   700),
    "SPY":     (50,  300,   700),
    "QQQ":     (50,  250,   700),
    "BTC-USD": (20, 8000, 250000),
    "ETH-USD": (20,  800,  20000),
}

# ── Dip thresholds ─────────────────────────────────────────────────────────────
# How far below the MA (%) triggers each alert level.
DIP_THRESHOLDS = {
    "error":   20,   # deep crash  — strong reversion candidate
    "warning": 10,   # notable dip — worth watching
    "info":     5,   # mild dip    — early signal
}

SCAN_INTERVAL      = 60   # seconds between full scans (be kind to Yahoo's API)
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
        pass   # hub not yet ready or temporarily down — silently retry next cycle


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

def _fetch(symbol: str, ma_period: int) -> tuple[float | None, float | None]:
    """
    Returns (live_price, simple_moving_average) or (None, None) on failure.

    live_price  — from ticker.fast_info.last_price   (real-time, not historical close)
    ma          — computed from the last `ma_period` daily closes via ticker.history()

    Using fast_info for the current price avoids the unit-mixup bug where
    yfinance sometimes returns adjusted closes from a non-primary exchange.
    """
    try:
        ticker = yf.Ticker(symbol)

        # ── Live price ──────────────────────────────────────────────────────
        fi    = ticker.fast_info
        price = getattr(fi, "last_price", None)

        if not price or price <= 0:
            # Fallback: last close from a 2-day history window
            df = ticker.history(period="2d", interval="1d")
            if df.empty:
                return None, None
            price = float(df["Close"].iloc[-1])

        # ── Historical closes for MA ─────────────────────────────────────────
        hist = ticker.history(period=f"{ma_period + 10}d", interval="1d")
        if hist.empty or len(hist) < ma_period:
            return None, None

        closes = hist["Close"].tolist()
        ma     = sum(closes[-ma_period:]) / ma_period

        return float(price), float(ma)

    except Exception:
        return None, None


# ── Scan logic ─────────────────────────────────────────────────────────────────

def _scan() -> None:
    for symbol, (ma_period, price_min, price_max) in ASSETS.items():

        price, ma = _fetch(symbol, ma_period)

        # ── Data fetch failed ────────────────────────────────────────────────
        if price is None:
            _post(
                f"{symbol}: could not fetch market data — will retry next scan",
                "warning",
                {"symbol": symbol},
            )
            _heartbeat()
            continue

        # ── Sanity check: price outside realistic range ──────────────────────
        if not (price_min <= price <= price_max):
            _post(
                f"{symbol}: fetched price {price:.2f} is outside expected range "
                f"{price_min}–{price_max} — skipping to avoid false signal",
                "warning",
                {"symbol": symbol, "fetched_price": price,
                 "expected_range": f"{price_min}–{price_max}"},
            )
            _heartbeat()
            continue

        # ── Dip calculation ──────────────────────────────────────────────────
        dip_pct = (ma - price) / ma * 100   # positive → price is below MA

        # Determine alert level (highest matching threshold wins)
        level = None
        for lvl, threshold in sorted(DIP_THRESHOLDS.items(), key=lambda x: -x[1]):
            if dip_pct >= threshold:
                level = lvl
                break

        payload = {
            "symbol":         symbol,
            "price":          round(price, 2),
            "ma":             round(ma,    2),
            "ma_period_days": ma_period,
            "dip_pct":        round(dip_pct, 2),
        }

        if level:
            # ── Dip detected ─────────────────────────────────────────────────
            direction = "below"
            _post(
                f"{symbol}  {dip_pct:.1f}% {direction} {ma_period}-day MA  "
                f"(price {price:.2f}  /  MA {ma:.2f})  — reversion candidate",
                level,
                payload,
            )
        else:
            # ── No signal — send a quiet status pulse ─────────────────────────
            if dip_pct >= 0:
                summary = (f"{symbol}  {dip_pct:.1f}% below MA  "
                           f"(price {price:.2f}  /  MA {ma:.2f})  — watching")
            else:
                summary = (f"{symbol}  {abs(dip_pct):.1f}% above MA  "
                           f"(price {price:.2f}  /  MA {ma:.2f})  — no signal")
            _post(summary, "info", payload)

        _heartbeat()
        time.sleep(1.5)   # small gap between tickers — avoids Yahoo rate-limiting


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    _wait_for_hub()

    _post(
        "Mean Reversion Bot online — scanning for BNF-style dips.",
        "info",
        {"assets": list(ASSETS.keys()), "thresholds_pct": DIP_THRESHOLDS},
    )

    while True:
        _scan()
        _heartbeat()
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
