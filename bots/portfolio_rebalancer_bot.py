#!/usr/bin/env python3
"""
portfolio_rebalancer_bot.py — Portfolio Rebalancer Advisor
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors your crypto / stock portfolio and alerts you when
drift exceeds your tolerance, with precise buy/sell amounts.

Requirements:
    For crypto:  pip install ccxt requests
    For stocks:  pip install yfinance requests
"""

import time
import requests
import yfinance as yf
import ccxt

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "portfolio_rebalancer_bot"
BOT_NAME = "Portfolio Rebalancer Bot"

# ═══════════════════════════════════════════════════════════════════════════════
# 🎯 USER CONFIG — EDIT THIS SECTION ════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

# Target weights (must sum to 1.0)
TARGET = {
    "BTC":  0.40,
    "ETH":  0.30,
    "USDT": 0.30,
}

# Your current holdings — update these whenever you trade outside the bot
HOLDINGS = {
    "BTC":  0.5,       # e.g. 0.5 BTC
    "ETH":  5.0,       # 5 ETH
    "USDT": 20000.0,   # stablecoin balance
}

# Drift tolerance (percentage points) — only alert if drift exceeds this
DRIFT_TOLERANCE = 2.0   # e.g. 2% drift

# Price source: "ccxt" (Binance public) or "yfinance" (for stocks like AAPL)
# For stocks, use yfinance.
PRICE_SOURCE = "ccxt"   # change to "yfinance" if your assets are stocks (e.g. AAPL)

# Scan interval
SCAN_INTERVAL = 120          # 2 minutes
HEARTBEAT_INTERVAL = 20

# ═══════════════════════════════════════════════════════════════════════════════
# END USER CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

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
        pass

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

def _wait_for_hub():
    for _ in range(60):
        try:
            if requests.get(HUB, timeout=2).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1)


# ── Price fetchers ────────────────────────────────────────────────────────────

def _fetch_prices_ccxt(assets):
    """Get last prices from Binance public API (no keys needed)."""
    binance = ccxt.binance()
    prices = {}
    for asset in assets:
        if asset == "USDT" or asset == "USD":
            prices[asset] = 1.0
            continue
        symbol = f"{asset}/USDT"
        try:
            ticker = binance.fetch_ticker(symbol)
            prices[asset] = ticker["last"]
        except Exception:
            _post(f"Could not get price for {symbol}", "warning")
            prices[asset] = None
    return prices

def _fetch_prices_yfinance(symbols):
    """Get last prices from Yahoo Finance (for stocks)."""
    prices = {}
    for sym in symbols:
        if sym.endswith("-USD"):
            clean = sym
        else:
            # Assume stock symbol like AAPL, MSFT
            pass
        try:
            ticker = yf.Ticker(sym)
            last = getattr(ticker.fast_info, "last_price", None)
            if not last:
                df = ticker.history(period="1d")
                if not df.empty:
                    last = float(df["Close"].iloc[-1])
            prices[sym] = last
        except Exception:
            _post(f"Could not get price for {sym}", "warning")
            prices[sym] = None
    return prices


# ── Rebalance logic ───────────────────────────────────────────────────────────

def _check_and_recommend():
    # Get current prices
    assets = list(HOLDINGS.keys())
    if PRICE_SOURCE == "ccxt":
        prices = _fetch_prices_ccxt(assets)
    else:
        prices = _fetch_prices_yfinance(assets)

    # Calculate current value per asset and total
    current_value = {}
    total_value = 0.0
    for asset, qty in HOLDINGS.items():
        price = prices.get(asset)
        if price is None:
            _post(f"{asset}: price unavailable, skipping rebalance", "warning")
            return
        val = qty * price
        current_value[asset] = val
        total_value += val

    if total_value == 0:
        return

    # Current weights
    current_weight = {a: v / total_value for a, v in current_value.items()}

    # Check drift
    drift_events = []
    for asset, target in TARGET.items():
        current = current_weight.get(asset, 0)
        drift_pct = (current - target) * 100  # percentage points
        if abs(drift_pct) > DRIFT_TOLERANCE:
            drift_events.append((asset, drift_pct, current, target))

    if not drift_events:
        return  # no rebalance needed

    # Generate recommendations
    for asset, drift, current, target in drift_events:
        price = prices[asset]
        if asset == "USDT" or price == 1.0:
            price = 1.0   # stablecoin

        # Dollar amount to buy/sell
        dollar_adjustment = total_value * (target - current)
        # Units to trade
        units = dollar_adjustment / price if price != 0 else 0

        side = "buy" if units > 0 else "sell"
        units_abs = abs(units)

        level = "info"
        if abs(drift) > 5:
            level = "error"
        elif abs(drift) > 3:
            level = "warning"

        _post(
            f"{asset}: drift {drift:+.1f}% → {side.upper()} {units_abs:.6f} "
            f"({abs(dollar_adjustment):.2f} USDT) to restore target {target*100:.0f}%",
            level,
            {
                "asset": asset,
                "drift_pct": round(drift, 2),
                "current_weight": round(current*100, 2),
                "target_weight": round(target*100, 2),
                "side": side,
                "units": round(units_abs, 6),
                "usdt_value": round(abs(dollar_adjustment), 2),
                "price": round(price, 2),
                "total_portfolio": round(total_value, 2)
            }
        )


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _wait_for_hub()
    _post(
        "Portfolio Rebalancer online — monitoring target allocation. "
        f"Tolerance: {DRIFT_TOLERANCE}% drift.",
        "info",
        {"targets": TARGET, "holdings": HOLDINGS}
    )

    while True:
        try:
            _check_and_recommend()
        except Exception as e:
            _post(f"Error: {e}", "error")
        _heartbeat()
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
