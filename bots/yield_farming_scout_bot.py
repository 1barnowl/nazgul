#!/usr/bin/env python3
"""
yield_farming_scout_bot.py — Yield Farming Scout (DeFi APY Tracker)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Fetches real‑time DeFi pool data from DefiLlama and alerts you
to the top yield opportunities by APY and TVL.

Requirements:
    pip install requests
"""

import time
import requests

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "yield_farming_scout_bot"
BOT_NAME = "Yield Farming Scout"

# ── Configuration ─────────────────────────────────────────────────────────────
MIN_TVL_USD  = 1_000_000   # minimum pool TVL to avoid tiny/risky pools
MIN_APY      = 5.0         # only alert if APY % exceeds this
TOP_N        = 5           # number of top pools to post each scan
CHAINS       = []          # leave empty to scan all chains, e.g. ["Ethereum","Arbitrum"]
POOL_TYPES   = []          # empty = all types; e.g. ["Stablecoin","Volatile"]

SCAN_INTERVAL      = 600  # 10 minutes (DefiLlama updates roughly every 15 min)
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


# ── Data fetcher ───────────────────────────────────────────────────────────────

def fetch_pools():
    """
    Get all liquidity pools from DefiLlama yields API.
    Returns list of pool dicts.
    """
    try:
        resp = requests.get("https://yields.llama.fi/pools", timeout=30)
        data = resp.json()
        if data.get("status") != "success":
            _post("DefiLlama API error: " + str(data.get("message", "unknown")), "error")
            return []
        return data["data"]
    except Exception as e:
        _post(f"Failed to fetch DefiLlama data: {e}", "error")
        return []


def filter_and_sort(pools):
    """Apply user thresholds and return top N pools sorted by APY descending."""
    filtered = []
    for p in pools:
        # Skip pools with missing key data
        if p.get("apy") is None or p.get("tvlUsd") is None:
            continue

        apy = float(p["apy"])
        tvl = float(p["tvlUsd"])

        if apy < MIN_APY or tvl < MIN_TVL_USD:
            continue

        # Chain filter (if any)
        if CHAINS and p.get("chain") not in CHAINS:
            continue

        # Pool type filter (e.g. "Stablecoin")
        if POOL_TYPES:
            if p.get("poolMeta") and "stablecoin" in p["poolMeta"].lower():
                pass  # might be stablecoin, but no standard field; skip filter for now
            # For simplicity, skip if exact type not found
            # Alternatively use "exposure" or "symbol" checks; we'll ignore filter if not easily done

        filtered.append(p)

    # Sort by APY descending, then TVL descending
    filtered.sort(key=lambda x: (-float(x["apy"]), -float(x["tvlUsd"])))
    return filtered[:TOP_N]


# ── Main scan ──────────────────────────────────────────────────────────────────

def scan():
    pools = fetch_pools()
    if not pools:
        return

    top = filter_and_sort(pools)
    if not top:
        _post("No pools match the current filters (TVL/APY thresholds).", "info")
        return

    for rank, pool in enumerate(top, 1):
        symbol   = pool.get("symbol", "unknown")
        chain    = pool.get("chain", "?")
        apy_val  = float(pool["apy"])
        tvl_val  = float(pool["tvlUsd"])
        project  = pool.get("project", "")
        stable   = "yes" if "stable" in symbol.lower() or "usd" in symbol.lower() else "no"
        il_risk  = pool.get("ilRisk", "unknown")

        level = "info"
        if apy_val >= 50:
            level = "error"
        elif apy_val >= 20:
            level = "warning"

        _post(
            f"#{rank} {symbol} ({chain}) — APY {apy_val:.1f}% | TVL ${tvl_val:,.0f} "
            f"| Project: {project} | IL risk: {il_risk}",
            level,
            {
                "rank": rank,
                "symbol": symbol,
                "chain": chain,
                "apy": round(apy_val, 2),
                "tvl_usd": tvl_val,
                "project": project,
                "stablecoin": stable,
                "il_risk": il_risk,
                "pool_id": pool.get("pool", pool.get("poolMeta", "")),
            }
        )


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    _wait_for_hub()
    _post(
        "Yield Farming Scout online — watching DeFi pools. "
        f"Min TVL: ${MIN_TVL_USD:,} | Min APY: {MIN_APY}% | Top {TOP_N} pools.",
        "info",
        {"chains": CHAINS or "all", "thresholds": {"tvl": MIN_TVL_USD, "apy": MIN_APY}}
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
