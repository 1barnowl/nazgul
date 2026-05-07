#!/usr/bin/env python3
"""
options_income_bot.py — Options Income Scanner
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Finds potential covered calls and cash‑secured puts using
real‑time options data from Yahoo Finance.

No real money is moved — this is an advisory scanner that
highlights attractive income opportunities.

Requirements:
    pip install yfinance requests
"""

import time
import requests
import yfinance as yf
from datetime import datetime, timedelta

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "options_income_bot"
BOT_NAME = "Options Income Bot"

# ── Symbols to watch ──────────────────────────────────────────────────────────
# Stocks that usually have liquid options
STOCKS = ["AAPL", "MSFT", "NVDA", "TSLA", "SPY", "QQQ"]

# ── Strategy thresholds ───────────────────────────────────────────────────────
MIN_PREMIUM_ANNUAL_CC  = 10.0   # minimum annualised return % for covered calls
MIN_PREMIUM_ANNUAL_CSP = 8.0    # minimum annualised return % for cash‑secured puts
MIN_OPEN_INTEREST      = 50     # ignore options with very low liquidity

SCAN_INTERVAL      = 300  # 5 minutes between full scans
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


# ── Options data fetcher ──────────────────────────────────────────────────────

def _fetch_options(symbol: str):
    """
    Returns (stock_price, expiration_dates, call_chain, put_chain).
    Chains are pandas DataFrames or empty if nothing available.
    """
    try:
        ticker = yf.Ticker(symbol)
        # Stock price
        price = getattr(ticker.fast_info, "last_price", None)
        if not price or price <= 0:
            df = ticker.history(period="1d")
            if not df.empty:
                price = float(df["Close"].iloc[-1])
            else:
                return None, [], None, None

        # Expiration dates
        exps = ticker.options
        if not exps:
            return price, [], None, None

        # Choose nearest expiration (but at least 3 days away to avoid 0 DTE noise)
        now = datetime.now()
        nearest_exp = None
        for e in sorted(exps):
            exp_date = datetime.strptime(e, "%Y-%m-%d")
            if (exp_date - now).days >= 3:
                nearest_exp = e
                break
        if not nearest_exp:
            return price, exps, None, None

        # Fetch chain for that expiration
        chain = ticker.option_chain(nearest_exp)
        calls = chain.calls
        puts  = chain.puts
        return price, [nearest_exp], calls, puts

    except Exception as e:
        _post(f"{symbol}: options fetch error: {e}", "warning")
        return None, [], None, None


def _annualised_return(premium: float, capital: float, days: int) -> float:
    """Annualised percentage return."""
    if capital <= 0 or days <= 0:
        return 0.0
    return (premium / capital) * (365.0 / days) * 100


# ── Strategy analyser ─────────────────────────────────────────────────────────

def _scan_covered_calls(symbol: str, stock_price: float, calls, days: int):
    """
    Find call options where selling a covered call gives a nice annualised yield.
    - Strike ≥ stock price (OTM or ATM)
    - Open interest ≥ MIN_OPEN_INTEREST
    - Calculate return = premium / stock_price, annualised
    """
    if calls is None or calls.empty:
        return []

    suggestions = []
    for _, row in calls.iterrows():
        strike = float(row["strike"])
        bid    = float(row["bid"])
        oi     = int(row.get("openInterest", 0) or 0)

        if bid <= 0 or oi < MIN_OPEN_INTEREST:
            continue
        if strike < stock_price * 0.98:  # deep ITM — skip (unlikely to be income strategy)
            continue

        premium   = bid * 100  # per contract, but we normalise to share
        capital   = stock_price * 100  # cost of 100 shares
        ann_return = _annualised_return(premium, capital, days)

        if ann_return >= MIN_PREMIUM_ANNUAL_CC:
            suggestions.append({
                "strategy":   "covered_call",
                "strike":     round(strike, 2),
                "bid":        round(bid, 2),
                "premium_usd": round(premium, 2),
                "capital_usd": round(capital, 2),
                "annual_return_pct": round(ann_return, 2),
                "days":       days,
                "open_interest": oi,
            })
    return suggestions


def _scan_secured_puts(symbol: str, stock_price: float, puts, days: int):
    """
    Find put options where selling a cash‑secured put gives a nice annualised yield.
    - Strike ≤ stock price (OTM/ATM)
    - Cash secured = strike * 100
    - Open interest ≥ MIN_OPEN_INTEREST
    """
    if puts is None or puts.empty:
        return []

    suggestions = []
    for _, row in puts.iterrows():
        strike = float(row["strike"])
        bid    = float(row["bid"])
        oi     = int(row.get("openInterest", 0) or 0)

        if bid <= 0 or oi < MIN_OPEN_INTEREST:
            continue
        if strike > stock_price * 1.02:  # deep OTM — not typical for income (prefer ATM/slight OTM)
            continue

        premium       = bid * 100
        capital       = strike * 100  # cash required to secure the put
        ann_return    = _annualised_return(premium, capital, days)

        if ann_return >= MIN_PREMIUM_ANNUAL_CSP:
            suggestions.append({
                "strategy":   "cash_secured_put",
                "strike":     round(strike, 2),
                "bid":        round(bid, 2),
                "premium_usd": round(premium, 2),
                "capital_usd": round(capital, 2),
                "annual_return_pct": round(ann_return, 2),
                "days":       days,
                "open_interest": oi,
            })
    return suggestions


def _scan():
    for symbol in STOCKS:
        stock_price, expirations, calls, puts = _fetch_options(symbol)
        if stock_price is None:
            _post(f"{symbol}: could not fetch stock price", "warning")
            continue
        if not expirations:
            _post(f"{symbol}: no option expirations found", "info")
            continue

        # Days to expiration
        exp_date = datetime.strptime(expirations[0], "%Y-%m-%d")
        days = (exp_date - datetime.now()).days
        if days <= 0:
            continue

        cc_suggestions  = _scan_covered_calls(symbol, stock_price, calls, days)
        csp_suggestions = _scan_secured_puts(symbol, stock_price, puts, days)

        # Post best suggestions (up to 3 each, sorted by return)
        for strat_type, suggestions in [("Call", cc_suggestions), ("Put", csp_suggestions)]:
            suggestions.sort(key=lambda x: x["annual_return_pct"], reverse=True)
            for rec in suggestions[:3]:
                level = "info"
                if rec["annual_return_pct"] >= 20:
                    level = "error"
                elif rec["annual_return_pct"] >= 15:
                    level = "warning"

                _post(
                    f"{symbol} {strat_type} Sell: Strike {rec['strike']}, "
                    f"Bid {rec['bid']}, {rec['annual_return_pct']}% ann., "
                    f"{rec['days']}d | Cap req {rec['capital_usd']}",
                    level,
                    payload=rec
                )

        # Heartbeat mixed in
        _heartbeat()
        time.sleep(2)  # small gap between stocks to avoid rate limits


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _wait_for_hub()
    _post(
        "Options Income Bot online — scanning real option chains for covered calls & cash‑secured puts.",
        "info",
        {"stocks": STOCKS, "min_cc_return": MIN_PREMIUM_ANNUAL_CC,
         "min_csp_return": MIN_PREMIUM_ANNUAL_CSP}
    )

    while True:
        _scan()
        _heartbeat()
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
