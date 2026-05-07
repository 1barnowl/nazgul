#!/usr/bin/env python3
"""
arbitrage_scanner_bot.py — Arbitrage Scanner Bot (Real CCXT)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Scans multiple exchanges for price discrepancies on the same asset.

Uses CCXT public tickers (bid / ask) to detect when a coin is
cheaper on one exchange and more expensive on another.

Requirements:
    pip install ccxt requests
"""

import time
import requests
import ccxt

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "arbitrage_scanner_bot"
BOT_NAME = "Arbitrage Scanner Bot"

# ── Configuration ─────────────────────────────────────────────────────────────
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

# Exchanges to scan — public ticker access is free on most.
# Some require API keys even for public data (e.g. Coinbase Pro).
# The bot will skip any exchange that cannot load.
EXCHANGES = [
    "binance",
    "bybit",
    "kraken",
    "okx",
    # "coinbasepro",   # requires API keys – enable only if keys are set
]

SCAN_INTERVAL      = 25   # seconds between full sweeps
HEARTBEAT_INTERVAL = 20

# ── Spread thresholds (after fee estimate) ────────────────────────────────────
# Fee estimate per side: 0.1% (adjust to your actual fee tier)
FEE_PCT  = 0.1      # per trade (i.e. 0.1% for taker)

# Alerts if net profit > threshold
THRESHOLDS = {
    "error":   2.0,   # extreme arb (>2% after fees)
    "warning": 1.0,   # strong arb (>1%)
    "info":    0.5,   # noticeable spread (>0.5%)
}

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


# ── Exchange initialisation ───────────────────────────────────────────────────
def init_exchanges():
    exchanges = {}
    for name in EXCHANGES:
        try:
            # Load exchange class dynamically from ccxt
            ex_class = getattr(ccxt, name)
            # Some exchanges need API keys even for public endpoints
            # We still try without; if it fails, we skip it.
            exchange = ex_class({
                'enableRateLimit': True,
                'options': {'defaultType': 'spot'},
            })
            # Quick connectivity test: load markets (cached)
            exchange.load_markets()
            exchanges[name] = exchange
            _post(f"Connected to {name}", "info")
        except Exception as e:
            _post(f"Could not connect to {name}: {e}", "warning")
    return exchanges


# ── Arbitrage scan ────────────────────────────────────────────────────────────
def scan(exchanges):
    for symbol in SYMBOLS:
        # Gather bid/ask from every exchange that has the symbol
        tickers = {}
        for name, ex in exchanges.items():
            if symbol not in ex.markets:
                continue
            try:
                ticker = ex.fetch_ticker(symbol)
                tickers[name] = {
                    "bid": ticker["bid"],
                    "ask": ticker["ask"],
                    "last": ticker["last"],
                }
            except Exception as e:
                _post(f"{name} {symbol} fetch error: {e}", "warning")
                continue

        if len(tickers) < 2:
            continue

        # Find best bid (highest) and best ask (lowest)
        best_bid_ex = max(tickers.items(), key=lambda x: x[1]["bid"])
        best_ask_ex = min(tickers.items(), key=lambda x: x[1]["ask"])

        highest_bid = best_bid_ex[1]["bid"]
        lowest_ask  = best_ask_ex[1]["ask"]

        if lowest_ask <= 0:
            continue

        gross_spread_pct = (highest_bid - lowest_ask) / lowest_ask * 100
        net_spread_pct   = gross_spread_pct - 2 * FEE_PCT   # buy fee + sell fee

        payload = {
            "symbol":           symbol,
            "lowest_ask_ex":    best_ask_ex[0],
            "lowest_ask":       lowest_ask,
            "highest_bid_ex":   best_bid_ex[0],
            "highest_bid":      highest_bid,
            "gross_spread_pct": round(gross_spread_pct, 3),
            "net_spread_pct":   round(net_spread_pct, 3),
        }

        # Determine alert level
        level = None
        for lvl, threshold in sorted(THRESHOLDS.items(), key=lambda x: -x[1]):
            if net_spread_pct >= threshold:
                level = lvl
                break

        if level:
            _post(
                f"{symbol}: Buy @ {lowest_ask:.2f} ({best_ask_ex[0]}) "
                f"→ Sell @ {highest_bid:.2f} ({best_bid_ex[0]}) "
                f"| Net spread {net_spread_pct:.2f}%",
                level,
                payload,
            )
        else:
            # Quiet status
            _post(
                f"{symbol}: Best spread {net_spread_pct:.2f}% "
                f"({best_ask_ex[0]}→{best_bid_ex[0]}) — no arb",
                "info",
                payload,
            )


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _wait_for_hub()
    exchanges = init_exchanges()

    if not exchanges:
        _post("No exchanges available. Bot idle.", "error")
        while True:
            _heartbeat()
            time.sleep(60)

    _post(
        f"Arbitrage Scanner online — {len(exchanges)} exchanges, "
        f"{len(SYMBOLS)} symbols. Fee estimate: {FEE_PCT}% per side.",
        "info",
        {"exchanges": list(exchanges.keys()), "symbols": SYMBOLS},
    )

    while True:
        try:
            scan(exchanges)
        except Exception as e:
            _post(f"Scan error: {e}", "error")
        _heartbeat()
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
