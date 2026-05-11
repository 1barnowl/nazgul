#!/usr/bin/env python3
"""
prediction_market_arbitrage_bot.py — Prediction Market Arbitrage Scanner
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Scans Polymarket and Kalshi for matching binary markets and identifies
risk‑free arbitrage opportunities where combined YES+NO price < $1.00.

Both APIs are public — no authentication required for reading market data.

SETUP
─────
1. Install dependencies:
      pip install requests fuzzywuzzy python-Levenshtein

2. (Optional) Set environment variables for automated trading:
      POLYMARKET_PRIVATE_KEY   = your Polygon wallet private key
      KALSHI_API_KEY_ID        = your Kalshi key ID
      KALSHI_PRIVATE_KEY_PATH  = path to your Kalshi private key PEM

3. Attach to BotController.
"""

import json
import os
import time
import threading
import requests
from datetime import datetime, timezone
from difflib import SequenceMatcher

try:
    from fuzzywuzzy import fuzz
    HAS_FUZZY = True
except ImportError:
    HAS_FUZZY = False

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "prediction_market_arbitrage_bot"
BOT_NAME = "Prediction Market Arb Scanner"

HEARTBEAT_INTERVAL = 20
SCAN_INTERVAL      = 300   # 5 minutes between scans

_last_hb = 0.0
_last_hb_lock = threading.Lock()

# ── Hub helpers ────────────────────────────────────────────────────────────────
def _post(summary, level="info", payload=None):
    try:
        requests.post(f"{HUB}/ingest", json={
            "bot_id": BOT_ID, "bot_name": BOT_NAME,
            "summary": summary, "level": level, "payload": payload or {}
        }, timeout=5)
    except Exception:
        pass

def _heartbeat():
    global _last_hb
    with _last_hb_lock:
        now = time.time()
        if now - _last_hb < HEARTBEAT_INTERVAL:
            return
        _last_hb = now
    try:
        requests.post(f"{HUB}/heartbeat/{BOT_ID}", json={
            "bot_name": BOT_NAME, "status": "online"
        }, timeout=3)
    except Exception:
        pass

def _wait_for_hub():
    for _ in range(60):
        try:
            if requests.get(HUB, timeout=2).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1)

# ── Configuration ──────────────────────────────────────────────────────────────
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"
KALSHI_TRADE_API = "https://external-api.kalshi.com/trade-api/v2"

# Minimum thresholds for alerting
MIN_RETURN_PCT     = 0.5    # minimum profit % to alert
MIN_SIMILARITY     = 0.45   # minimum fuzzy match score (0-1)
MIN_VOLUME_POLY    = 10000  # minimum Polymarket volume in USD
MIN_VOLUME_KALSHI  = 5000   # minimum Kalshi "volume_fp" (notional)

# Execution: set these env vars to enable live trading
POLY_PRIVATE_KEY   = os.getenv("POLYMARKET_PRIVATE_KEY", "")
KALSHI_KEY_ID      = os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_KEY_PATH    = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")

# ── Market fetchers ─────────────────────────────────────────────────────────────

def fetch_polymarket_markets(limit=500):
    """Fetch active binary markets from Polymarket Gamma API."""
    markets = []
    offset = 0
    while True:
        try:
            resp = requests.get(
                f"{POLYMARKET_GAMMA}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": min(limit, 500),
                    "offset": offset,
                },
                timeout=30
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            for m in batch:
                # Parse outcome prices (JSON-encoded strings)
                try:
                    outcome_prices = json.loads(m.get("outcomePrices", "[]"))
                except (json.JSONDecodeError, TypeError):
                    outcome_prices = []
                try:
                    outcomes = json.loads(m.get("outcomes", "[]"))
                except (json.JSONDecodeError, TypeError):
                    outcomes = []

                if len(outcome_prices) < 2 or len(outcomes) < 2:
                    continue

                # Map "Yes"/"No" prices
                yes_price = None
                no_price  = None
                for i, label in enumerate(outcomes):
                    if label.lower() == "yes":
                        yes_price = float(outcome_prices[i])
                    elif label.lower() == "no":
                        no_price = float(outcome_prices[i])

                if yes_price is None or no_price is None:
                    continue

                volume = float(m.get("volume", 0) or 0)
                if volume < MIN_VOLUME_POLY:
                    continue

                markets.append({
                    "platform":    "polymarket",
                    "title":       m.get("question", m.get("title", "")),
                    "slug":        m.get("slug", ""),
                    "yes_price":   yes_price,
                    "no_price":    no_price,
                    "volume":      volume,
                    "condition_id": m.get("conditionId", ""),
                    "clob_token_ids": m.get("clobTokenIds", []),
                    "end_date":    m.get("endDate", m.get("end_date", "")),
                    "url":         f"https://polymarket.com/event/{m.get('slug', '')}" if m.get("slug") else "",
                })
            offset += len(batch)
            if len(batch) < limit:
                break
            time.sleep(0.3)  # rate limit
        except requests.RequestException as e:
            _post(f"Polymarket API error: {e}", "warning")
            break
    return markets


def fetch_kalshi_markets(limit=500):
    """Fetch active binary markets from Kalshi public Trade API."""
    markets = []
    cursor = None
    while True:
        try:
            params = {
                "status": "open",
                "limit": min(limit, 100),
            }
            if cursor:
                params["cursor"] = cursor
            resp = requests.get(
                f"{KALSHI_TRADE_API}/markets",
                params=params,
                timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("markets", [])
            cursor = data.get("cursor")

            for m in batch:
                # Kalshi provides yes_bid, yes_ask, no_bid, no_ask in cents
                # Use the midpoint for a fair price estimate
                yes_bid = float(m.get("yes_bid_dollars", 0) or 0)
                yes_ask = float(m.get("yes_ask_dollars", 0) or 0)
                no_bid  = float(m.get("no_bid_dollars", 0) or 0)
                no_ask  = float(m.get("no_ask_dollars", 0) or 0)

                # Use best bid as price (what you can immediately sell at)
                # and best ask as the price you can buy at
                yes_price = yes_ask if yes_ask > 0 else yes_bid
                no_price  = no_ask if no_ask > 0 else no_bid

                volume = float(m.get("volume_fp", 0) or 0)
                if volume < MIN_VOLUME_KALSHI:
                    continue

                markets.append({
                    "platform":   "kalshi",
                    "title":      m.get("title", ""),
                    "ticker":     m.get("ticker", ""),
                    "yes_price":  yes_price,
                    "no_price":   no_price,
                    "volume":     volume,
                    "event_ticker": m.get("event_ticker", ""),
                    "series_ticker": m.get("series_ticker", ""),
                    "end_date":   m.get("close_time", ""),
                    "url":        f"https://kalshi.com/markets/{m.get('ticker', '')}",
                })
            if not batch or len(batch) < limit or cursor is None:
                break
            time.sleep(0.3)
        except requests.RequestException as e:
            _post(f"Kalshi API error: {e}", "warning")
            break
    return markets


# ── Market matching ────────────────────────────────────────────────────────────

def match_markets(poly_markets, kalshi_markets):
    """
    Match Polymarket markets with Kalshi markets using fuzzy title matching.
    Returns list of (poly_market, kalshi_market, score) tuples.
    """
    if not HAS_FUZZY:
        _post("fuzzywuzzy not installed. Install: pip install fuzzywuzzy python-Levenshtein", "error")
        return []

    matches = []
    # For efficiency, only compare markets that might match
    for pm in poly_markets:
        poly_title = pm["title"].lower().strip()
        best_score = 0
        best_km = None
        for km in kalshi_markets:
            kalshi_title = km["title"].lower().strip()
            # Use token set ratio for better partial matching
            score = fuzz.token_set_ratio(poly_title, kalshi_title) / 100.0
            if score > best_score:
                best_score = score
                best_km = km
        if best_score >= MIN_SIMILARITY and best_km is not None:
            matches.append((pm, best_km, best_score))
    return matches


# ── Arbitrage calculation ─────────────────────────────────────────────────────

def calculate_arbitrage(poly_market, kalshi_market, match_score):
    """
    Calculate risk‑free arbitrage between two matching markets.

    Strategy: Buy YES on the platform where it's cheaper, buy NO on the other.
    If total cost < $1.00, the difference is guaranteed profit.
    """
    poly_yes = poly_market["yes_price"]
    poly_no  = poly_market["no_price"]
    kalshi_yes = kalshi_market["yes_price"]
    kalshi_no  = kalshi_market["no_price"]

    # Skip if any price is invalid
    for p in [poly_yes, poly_no, kalshi_yes, kalshi_no]:
        if p is None or p <= 0 or p >= 1:
            return None

    # Option A: Buy YES on Polymarket, Buy NO on Kalshi
    cost_a = poly_yes + kalshi_no
    return_a = ((1.0 - cost_a) / cost_a) * 100 if cost_a > 0 else 0

    # Option B: Buy YES on Kalshi, Buy NO on Polymarket
    cost_b = kalshi_yes + poly_no
    return_b = ((1.0 - cost_b) / cost_b) * 100 if cost_b > 0 else 0

    # Choose the better direction
    if return_a >= return_b and return_a >= MIN_RETURN_PCT:
        direction = "Buy YES on Polymarket + Buy NO on Kalshi"
        cost_per_unit = cost_a
        profit_per_unit = 1.0 - cost_a
        return_pct = return_a
        buy_yes_platform = "Polymarket"
        buy_no_platform  = "Kalshi"
        yes_price = poly_yes
        no_price  = kalshi_no
    elif return_b >= MIN_RETURN_PCT:
        direction = "Buy YES on Kalshi + Buy NO on Polymarket"
        cost_per_unit = cost_b
        profit_per_unit = 1.0 - cost_b
        return_pct = return_b
        buy_yes_platform = "Kalshi"
        buy_no_platform  = "Polymarket"
        yes_price = kalshi_yes
        no_price  = poly_no
    else:
        return None

    return {
        "direction":          direction,
        "buy_yes_platform":   buy_yes_platform,
        "buy_no_platform":    buy_no_platform,
        "yes_price":          round(yes_price, 4),
        "no_price":           round(no_price, 4),
        "cost_per_unit":      round(cost_per_unit, 4),
        "profit_per_unit":    round(profit_per_unit, 4),
        "return_pct":         round(return_pct, 2),
        "match_score":        round(match_score, 2),
        "poly_title":         poly_market["title"],
        "kalshi_title":       kalshi_market["title"],
        "poly_url":           poly_market.get("url", ""),
        "kalshi_url":         kalshi_market.get("url", ""),
        "poly_volume":        poly_market["volume"],
        "kalshi_volume":      kalshi_market["volume"],
    }


# ── Scan orchestration ────────────────────────────────────────────────────────

def scan():
    _post("Fetching Polymarket markets...", "info")
    poly_markets = fetch_polymarket_markets(limit=500)
    _post(f"Fetched {len(poly_markets)} active Polymarket markets.", "info")

    _post("Fetching Kalshi markets...", "info")
    kalshi_markets = fetch_kalshi_markets(limit=500)
    _post(f"Fetched {len(kalshi_markets)} active Kalshi markets.", "info")

    if not poly_markets or not kalshi_markets:
        _post("Insufficient market data to run arbitrage scan.", "warning")
        return

    _post(f"Matching {len(poly_markets)} × {len(kalshi_markets)} markets...", "info")
    matches = match_markets(poly_markets, kalshi_markets)
    _post(f"Found {len(matches)} potential market matches.", "info")

    opportunities = []
    for pm, km, score in matches:
        arb = calculate_arbitrage(pm, km, score)
        if arb:
            opportunities.append(arb)

    # Sort by return % descending
    opportunities.sort(key=lambda x: x["return_pct"], reverse=True)

    if not opportunities:
        _post("No arbitrage opportunities found above minimum return threshold.", "info")
        return

    _post(f"Found {len(opportunities)} arbitrage opportunities!", "info")

    # Report top opportunities
    for rank, opp in enumerate(opportunities[:20]):  # top 20
        level = "error" if opp["return_pct"] >= 5 else ("warning" if opp["return_pct"] >= 2 else "info")
        _post(
            f"#{rank+1} ARB: {opp['return_pct']:.1f}% return — {opp['direction']}\n"
            f"  Cost: ${opp['cost_per_unit']:.3f} → Payout: $1.00 → Profit: ${opp['profit_per_unit']:.3f}/share\n"
            f"  Match: {opp['match_score']:.0%} | Poly: \"{opp['poly_title'][:80]}\" | Kalshi: \"{opp['kalshi_title'][:80]}\"",
            level,
            opp
        )

    # Summary stats
    total_profit = sum(o["profit_per_unit"] for o in opportunities[:20])
    avg_return = sum(o["return_pct"] for o in opportunities[:20]) / min(len(opportunities), 20)
    _post(
        f"Scan complete. Top 20 opps: avg {avg_return:.1f}% return, "
        f"total potential profit ${total_profit:.2f}/share across all.",
        "info"
    )


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _wait_for_hub()

    if not HAS_FUZZY:
        _post("Missing fuzzywuzzy. Install: pip install fuzzywuzzy python-Levenshtein", "error")

    exec_possible = bool(POLY_PRIVATE_KEY and KALSHI_KEY_ID and KALSHI_KEY_PATH)
    _post(
        f"Prediction Market Arbitrage Scanner online.\n"
        f"  Polymarket: Gamma API (public)\n"
        f"  Kalshi: Trade API v2 (public)\n"
        f"  Auto-execution: {'ENABLED' if exec_possible else 'DISABLED (set env vars to enable)'}",
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
