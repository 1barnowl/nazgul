#!/usr/bin/env python3
"""
ebay_sniper_flagger_bot.py — eBay Auction Sniper & Flagger Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Places last‑second bids on eBay auctions and flags suspicious
listings for removal. Snipe windows and suspicion criteria are
configurable.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests requests-oauthlib

Configuration
─────────────
Place `ebay_sniper_config.json` in the same directory:

{
  "ebay": {
    "client_id": "YOUR_CLIENT_ID",
    "client_secret": "YOUR_CLIENT_SECRET",
    "refresh_token": "YOUR_REFRESH_TOKEN",
    "sandbox": true
  },
  "sniping": {
    "seconds_before_end": 5,
    "max_bid_default": 100.00
  },
  "monitored_items": [
    {
      "item_id": "123456789012",
      "max_bid": 150.00
    }
  ],
  "search_keywords": ["nintendo switch oled"],
  "suspicion": {
    "enabled": true,
    "max_price": 20.00,
    "suspicious_keywords": ["cheap", "fake", "replica", "not real"],
    "reason": "Suspected counterfeit or misleading listing"
  },
  "poll_interval_seconds": 10,
  "state_file": "ebay_sniper_state.json",
  "heartbeat_interval": 30
}
"""

import json
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import requests
from requests_oauthlib import OAuth2Session

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "ebay_sniper_flagger_bot"
BOT_NAME = "eBay Sniper & Flagger"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "ebay_sniper_config.json"
CONFIG_PATH = Path(__file__).with_name(CONFIG_NAME)
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(CONFIG_NAME)

# ── Hub helpers ──────────────────────────────────────────────────
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

# ── State persistence ────────────────────────────────────────────
def load_state(state_file: str) -> dict:
    try:
        with open(state_file, "r") as f:
            return json.load(f)
    except Exception:
        return {"flagged_items": [], "sniped_items": []}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── eBay client ──────────────────────────────────────────────────
class eBayClient:
    def __init__(self, config: dict):
        ebay_cfg = config.get("ebay", {})
        self.client_id = ebay_cfg["client_id"]
        self.client_secret = ebay_cfg["client_secret"]
        self.refresh_token = ebay_cfg["refresh_token"]
        self.sandbox = ebay_cfg.get("sandbox", True)
        self.base_url = "https://api.sandbox.ebay.com" if self.sandbox else "https://api.ebay.com"
        self.token = None

    def get_token(self) -> str:
        """Obtain or refresh OAuth token."""
        token_url = "https://api.sandbox.ebay.com/identity/v1/oauth2/token" if self.sandbox else "https://api.ebay.com/identity/v1/oauth2/token"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": "Basic " + requests.auth._basic_auth_str(self.client_id, self.client_secret)
        }
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token
        }
        try:
            resp = requests.post(token_url, headers=headers, data=data, timeout=10)
            if resp.status_code == 200:
                token_data = resp.json()
                self.token = token_data["access_token"]
                return self.token
            else:
                raise Exception(f"Token refresh failed: {resp.text}")
        except Exception as e:
            _post(f"eBay auth error: {e}", "error")
            raise

    def get_item(self, item_id: str) -> Optional[dict]:
        """Fetch item details via Trading API (GetItem)."""
        if not self.token:
            self.get_token()
        endpoint = f"{self.base_url}/ws/api.dll"
        headers = {
            "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
            "X-EBAY-API-CALL-NAME": "GetItem",
            "X-EBAY-API-IAF-TOKEN": self.token,   # or use Authorization header?
            "Content-Type": "text/xml"
        }
        body = f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <ItemID>{item_id}</ItemID>
</GetItemRequest>"""
        try:
            resp = requests.post(endpoint, data=body, headers=headers, timeout=10)
            if resp.status_code == 200:
                root = ET.fromstring(resp.text)
                ns = {'urn': 'urn:ebay:apis:eBLBaseComponents'}
                ack = root.find('.//urn:Ack', ns).text
                if ack == 'Success':
                    item = root.find('.//urn:Item', ns)
                    end_time_str = item.find('urn:ListingDetails/urn:EndTime', ns).text
                    current_price = item.find('urn:SellingStatus/urn:CurrentPrice', ns).text
                    title = item.find('urn:Title', ns).text
                    return {
                        "item_id": item_id,
                        "title": title,
                        "end_time": end_time_str,
                        "current_price": float(current_price),
                    }
            return None
        except Exception as e:
            _post(f"GetItem failed for {item_id}: {e}", "warning")
            return None

    def place_bid(self, item_id: str, max_bid: float) -> bool:
        """Place a maximum bid using PlaceOffer."""
        if not self.token:
            self.get_token()
        endpoint = f"{self.base_url}/ws/api.dll"
        headers = {
            "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
            "X-EBAY-API-CALL-NAME": "PlaceOffer",
            "X-EBAY-API-IAF-TOKEN": self.token,
            "Content-Type": "text/xml"
        }
        body = f"""<?xml version="1.0" encoding="utf-8"?>
<PlaceOfferRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <Offer>
    <Action>Bid</Action>
    <ItemID>{item_id}</ItemID>
    <MaxBid currencyID="USD">{max_bid:.2f}</MaxBid>
    <Quantity>1</Quantity>
  </Offer>
</PlaceOfferRequest>"""
        try:
            resp = requests.post(endpoint, data=body, headers=headers, timeout=10)
            if resp.status_code == 200 and '<Ack>Success</Ack>' in resp.text:
                return True
            else:
                _post(f"Bid failed for {item_id}: {resp.text[:200]}", "error")
                return False
        except Exception as e:
            _post(f"PlaceOffer error: {e}", "error")
            return False

    def report_item(self, item_id: str, reason: str) -> bool:
        """Report a listing via Sell API Item Report."""
        if not self.token:
            self.get_token()
        endpoint = f"{self.base_url}/sell/listing/v1_beta/item_report"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        payload = {
            "itemId": item_id,
            "reason": "SUSPICIOUS_LISTING",
            "description": reason
        }
        try:
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=10)
            if resp.status_code in (200, 201, 204):
                return True
            else:
                _post(f"Report failed for {item_id}: {resp.text[:200]}", "warning")
                return False
        except Exception as e:
            _post(f"Report API error: {e}", "warning")
            return False

# ── Suspicion detection ────────────────────────────────────────
def is_suspicious(item: dict, config: dict) -> Optional[str]:
    """Return a reason string if the item matches suspicion rules, else None."""
    if not config.get("enabled", False):
        return None
    max_price = float(config.get("max_price", 20.0))
    title_lower = item.get("title", "").lower()
    current_price = item.get("current_price", 0.0)
    if current_price < max_price:
        return f"Price too low (${current_price:.2f} < ${max_price:.2f})"
    for kw in config.get("suspicious_keywords", []):
        if kw.lower() in title_lower:
            return f"Contains suspicious keyword: {kw}"
    # Could add more checks (e.g., seller feedback, location) but beyond scope
    return None

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("eBay Sniper & Flagger Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        client = eBayClient(config)
        state_file = config.get("state_file", "ebay_sniper_state.json")
        state = load_state(state_file)
        flagged_set = set(state.get("flagged_items", []))
        sniped_set = set(state.get("sniped_items", []))

        sniping_cfg = config.get("sniping", {})
        seconds_before = int(sniping_cfg.get("seconds_before_end", 5))
        default_max_bid = float(sniping_cfg.get("max_bid_default", 100.0))

        suspicion_cfg = config.get("suspicion", {})

        monitored = config.get("monitored_items", [])
        search_keywords = config.get("search_keywords", [])

        poll_interval = int(config.get("poll_interval_seconds", 10))

        while True:
            try:
                # 1. Search for items ending soon if keywords given
                item_ids_to_check = set()
                if search_keywords:
                    # Use Finding API to find items ending within 1 minute
                    # We'll keep it simple: the bot just scans pre‑configured item IDs.
                    # Real implementation would use findItemsAdvanced and filter by endTime.
                    pass

                # Add monitored items
                for m in monitored:
                    item_ids_to_check.add(m["item_id"])

                # Also could add items found from search (placeholder for real integration)

                for item_id in item_ids_to_check:
                    item = client.get_item(item_id)
                    if not item:
                        continue

                    # ── Suspicion check ─────────────────────────
                    if item_id not in flagged_set:
                        reason = is_suspicious(item, suspicion_cfg)
                        if reason:
                            if client.report_item(item_id, reason):
                                flagged_set.add(item_id)
                                _post(f"Flagged suspicious listing {item_id} – {reason}", "warning", {"item_id": item_id})
                            else:
                                _post(f"Failed to flag {item_id}", "error")

                    # ── Sniping check ───────────────────────────
                    if item_id in sniped_set:
                        continue

                    # Determine if auction is within sniping window
                    try:
                        end_time = datetime.fromisoformat(item["end_time"])
                    except Exception:
                        _post(f"Bad date for {item_id}", "warning")
                        continue
                    now = datetime.now(timezone.utc)
                    seconds_left = (end_time - now).total_seconds()

                    if 0 < seconds_left <= seconds_before:
                        # Find matching monitored item for max bid
                        max_bid = default_max_bid
                        for m in monitored:
                            if m["item_id"] == item_id:
                                max_bid = float(m.get("max_bid", default_max_bid))
                                break

                        # Only bid if current price < max_bid
                        if item["current_price"] < max_bid:
                            if client.place_bid(item_id, max_bid):
                                sniped_set.add(item_id)
                                _post(f"Placed bid on {item_id} at ${max_bid:.2f} (currently ${item['current_price']:.2f})",
                                      "error", {"item_id": item_id, "bid": max_bid})
                            else:
                                _post(f"Failed to bid on {item_id}", "error")
                        else:
                            _post(f"Bid skipped for {item_id}: current price {item['current_price']} >= max {max_bid}", "info")

                # Save state
                state["flagged_items"] = list(flagged_set)
                state["sniped_items"] = list(sniped_set)
                save_state(state_file, state)

            except Exception as e:
                _post(f"Error in main cycle: {e}", "error")

            _heartbeat()
            time.sleep(poll_interval)

if __name__ == "__main__":
    main()
