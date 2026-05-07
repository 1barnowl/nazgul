#!/usr/bin/env python3
"""
tax_loss_harvester_bot.py — Tax-Loss Harvesting Advisor
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Scans your real portfolio for unrealised losses, checks
wash‑sale rules (US), and recommends sale candidates.

━━━━━━━━━━━━━━━━━━ SETUP ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Create a file named `tax_loss_config.json` next to this bot:

{
  "portfolio": [
    {
      "symbol": "AAPL",
      "quantity": 10,
      "cost_per_share": 185.00,
      "purchase_date": "2025-03-15"
    },
    {
      "symbol": "NVDA",
      "quantity": 5,
      "cost_per_share": 800.00,
      "purchase_date": "2025-01-10"
    }
  ],
  "wash_sale_window_days": 30
}

Requirements:
    pip install yfinance requests
"""

import json
import os
import time
import requests
import yfinance as yf
from datetime import datetime, timedelta

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "tax_loss_harvester_bot"
BOT_NAME = "Tax-Loss Harvester"

# ── File paths ────────────────────────────────────────────────────────────────
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "tax_loss_config.json")

# ── Timing ────────────────────────────────────────────────────────────────────
SCAN_INTERVAL      = 300  # 5 minutes
HEARTBEAT_INTERVAL = 20

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


# ── Config loader ─────────────────────────────────────────────────────────────
def load_portfolio():
    if not os.path.exists(CONFIG_FILE):
        _post("Config file not found. Create tax_loss_config.json.", "error")
        return None, 30

    try:
        with open(CONFIG_FILE, "r") as f:
            cfg = json.load(f)
        portfolio = cfg.get("portfolio", [])
        window = cfg.get("wash_sale_window_days", 30)
        return portfolio, window
    except Exception as e:
        _post(f"Config error: {e}", "error")
        return None, 30


# ── Price fetch ────────────────────────────────────────────────────────────────
def _fetch_prices(symbols):
    prices = {}
    for sym in symbols:
        try:
            ticker = yf.Ticker(sym)
            price = getattr(ticker.fast_info, "last_price", None)
            if not price or price <= 0:
                df = ticker.history(period="1d")
                if not df.empty:
                    price = float(df["Close"].iloc[-1])
            prices[sym] = price if price and price > 0 else None
        except Exception:
            prices[sym] = None
    return prices


# ── Harvest scan ───────────────────────────────────────────────────────────────
def scan():
    portfolio, window = load_portfolio()
    if not portfolio:
        return

    symbols = [h["symbol"] for h in portfolio]
    prices  = _fetch_prices(symbols)

    today = datetime.now().date()
    harvest_candidates = []

    for holding in portfolio:
        sym        = holding["symbol"]
        qty        = float(holding["quantity"])
        cost       = float(holding["cost_per_share"])
        buy_date   = datetime.strptime(holding["purchase_date"], "%Y-%m-%d").date()

        current_price = prices.get(sym)
        if current_price is None:
            _post(f"{sym}: price unavailable", "warning")
            continue

        # Unrealised gain/loss
        change_pct = (current_price - cost) / cost * 100
        if change_pct >= 0:
            continue  # only losses matter

        # Holding period
        holding_days = (today - buy_date).days
        is_long_term = holding_days >= 365

        # ── Wash‑sale check ──────────────────────────────────────────────
        wash_flag = False
        if holding_days <= window:
            # Bought within the wash‑sale window before today → can't sell yet
            wash_flag = True

        # Also check: did you buy the same asset multiple times recently?
        # Only consider this holding's own purchase
        # (If there were multiple lots, you'd need a tax professional.)

        candidate = {
            "symbol":     sym,
            "quantity":   qty,
            "cost":       cost,
            "price":      current_price,
            "loss_pct":   round(change_pct, 2),
            "loss_usd":   round((cost - current_price) * qty, 2),
            "holding_days": holding_days,
            "long_term":  is_long_term,
            "wash_sale_risk": wash_flag,
        }
        harvest_candidates.append(candidate)

    if not harvest_candidates:
        return

    # Sort by largest loss first
    harvest_candidates.sort(key=lambda x: x["loss_pct"])

    for c in harvest_candidates:
        level = "info"
        if c["long_term"] and abs(c["loss_pct"]) > 15:
            level = "warning"
        elif not c["long_term"] and abs(c["loss_pct"]) > 10:
            level = "error"

        wash_msg = ""
        if c["wash_sale_risk"]:
            wash_msg = " ⚠️ WASH SALE RISK (bought within 30 days) – do NOT sell yet"
            level = "warning"

        _post(
            f"{c['symbol']}: {c['loss_pct']:+.1f}% loss "
            f"({c['loss_usd']:.2f} USD unrealised) "
            f"{'LONG' if c['long_term'] else 'SHORT'} {c['holding_days']}d hold"
            f"{wash_msg}",
            level,
            c
        )


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _wait_for_hub()
    _post(
        "Tax‑Loss Harvester online — monitoring portfolio losses & wash sale rules.",
        "info"
    )

    while True:
        try:
            scan()
        except Exception as e:
            _post(f"Scan error: {e}", "error")
        _heartbeat()
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
