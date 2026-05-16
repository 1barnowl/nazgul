#!/usr/bin/env python3
"""
cross_platform_listing_sync_bot.py — Cross‑Platform Listing Sync Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Synchronises product listings across multiple marketplaces
(eBay, Amazon, Etsy, etc.) from a single inventory feed.
Detects changes (price, quantity, title) and pushes updates
to the respective marketplace APIs.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests ebay-oauth boto3 python-amazon-sp-api

Configuration
─────────────
Place `listing_sync_config.json` in the same directory:

{
  "inventory_file": "inventory.json",
  "state_file": "listing_sync_state.json",
  "poll_interval_seconds": 300,
  "marketplaces": {
    "ebay": {
      "client_id": "...",
      "client_secret": "...",
      "refresh_token": "...",
      "sandbox": false
    },
    "amazon": {
      "refresh_token": "...",
      "lwa_app_id": "...",
      "lwa_client_secret": "...",
      "aws_access_key": "...",
      "aws_secret_key": "...",
      "role_arn": "...",
      "region": "na"
    },
    "etsy": {
      "access_token": "...",
      "shop_id": "..."
    }
  },
  "heartbeat_interval": 30
}

Inventory file format (inventory.json) – an array of objects:
[
  {
    "sku": "TEE-RED-M",
    "title": "Classic Red Tee – Medium",
    "description": "Comfortable red cotton tee.",
    "price": 19.99,
    "quantity": 15,
    "ebay_item_id": "123456789012",
    "amazon_sku": "TEE-RED-M-AMZ",
    "etsy_listing_id": 987654321
  },
  ...
]
"""

import json
import os
import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "cross_platform_listing_sync_bot"
BOT_NAME = "Cross‑Platform Listing Sync"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "listing_sync_config.json"
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

# ── State management ────────────────────────────────────────────
def load_state(state_file: str) -> dict:
    try:
        with open(state_file, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state_file: str, state: dict):
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

def item_fingerprint(item: dict) -> str:
    """Create a unique hash for the important fields of an item."""
    fields_to_hash = [
        str(item.get("title", "")),
        str(item.get("description", "")),
        str(item.get("price", 0.0)),
        str(item.get("quantity", 0))
    ]
    raw = "|".join(fields_to_hash)
    return hashlib.sha256(raw.encode()).hexdigest()

# ── Marketplace API adapters ─────────────────────────────────────

def ebay_update_listing(item: dict, config: dict) -> bool:
    """Update an eBay listing via the Trading API (ReviseFixedPriceItem)."""
    try:
        from ebay_oauth.token import OAuthToken
        from requests_oauthlib import OAuth2Session
    except ImportError:
        _post("ebay-oauth not installed. Install with: pip install ebay-oauth", "error")
        return False

    ebay_cfg = config.get("ebay", {})
    client_id = ebay_cfg.get("client_id")
    client_secret = ebay_cfg.get("client_secret")
    refresh_token = ebay_cfg.get("refresh_token")
    sandbox = ebay_cfg.get("sandbox", False)

    if not all([client_id, client_secret, refresh_token]):
        _post("eBay credentials incomplete", "warning")
        return False

    token = OAuthToken(client_id, client_secret)
    try:
        access_token_data = token.getAccessToken(None, None, refresh_token)
        access_token = access_token_data['access_token']
    except Exception as e:
        _post(f"eBay OAuth error: {e}", "error")
        return False

    item_id = item.get("ebay_item_id")
    if not item_id:
        return False

    # Build the Trading API XML request
    url = "https://api.ebay.com/ws/api.dll"
    headers = {
        "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
        "X-EBAY-API-CALL-NAME": "ReviseFixedPriceItem",
        "X-EBAY-API-SITEID": "0",
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "text/xml"
    }

    body = f"""<?xml version="1.0" encoding="utf-8"?>
<ReviseFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{access_token}</eBayAuthToken>
  </RequesterCredentials>
  <Item>
    <ItemID>{item_id}</ItemID>
    <Title>{item.get('title', '')[:80]}</Title>
    <Description>{item.get('description', '')[:5000]}</Description>
    <StartPrice currencyID="USD">{item.get('price', 0.0)}</StartPrice>
    <Quantity>{int(item.get('quantity', 0))}</Quantity>
  </Item>
</ReviseFixedPriceItemRequest>"""
    try:
        resp = requests.post(url, data=body, headers=headers, timeout=10)
        if resp.status_code == 200 and "<Ack>Success</Ack>" in resp.text:
            return True
        else:
            _post(f"eBay revise failed for item {item_id}: {resp.text[:200]}", "warning")
            return False
    except Exception as e:
        _post(f"eBay API error: {e}", "error")
        return False

def amazon_update_listing(item: dict, config: dict) -> bool:
    """Update an Amazon listing via SP‑API (Listings Items API)."""
    try:
        from sp_api.api import ListingsItems
        from sp_api.base import Marketplaces
    except ImportError:
        _post("python-amazon-sp-api not installed. Install with: pip install python-amazon-sp-api", "error")
        return False

    amz_cfg = config.get("amazon", {})
    if not all([amz_cfg.get("refresh_token"), amz_cfg.get("lwa_app_id"),
                amz_cfg.get("lwa_client_secret"), amz_cfg.get("aws_access_key"),
                amz_cfg.get("aws_secret_key"), amz_cfg.get("role_arn")]):
        _post("Amazon SP‑API credentials incomplete", "warning")
        return False

    amazon_sku = item.get("amazon_sku")
    if not amazon_sku:
        return False

    try:
        listings = ListingsItems(
            credentials={
                "refresh_token": amz_cfg["refresh_token"],
                "lwa_app_id": amz_cfg["lwa_app_id"],
                "lwa_client_secret": amz_cfg["lwa_client_secret"],
                "aws_access_key": amz_cfg["aws_access_key"],
                "aws_secret_key": amz_cfg["aws_secret_key"],
                "role_arn": amz_cfg["role_arn"]
            },
            marketplace=Marketplaces.US
        )
        response = listings.put_listings_item(
            sellerId="YOUR_SELLER_ID",  # In practice you’d need to fetch this, but we'll assume provided or static
            sku=amazon_sku,
            body={
                "productType": "PRODUCT",
                "patches": [
                    {"op": "replace", "path": "/attributes/fulfillment_availability",
                     "value": {"fulfillment_channel_code": "DEFAULT", "quantity": int(item.get("quantity", 0))}},
                    {"op": "replace", "path": "/attributes/price",
                     "value": {"price": float(item.get("price", 0.0)), "currency_code": "USD"}},
                    {"op": "replace", "path": "/attributes/title",
                     "value": item.get("title", "")}
                ]
            }
        )
        if response.errors:
            _post(f"Amazon SP‑API error for {amazon_sku}: {response.errors}", "warning")
            return False
        return True
    except Exception as e:
        _post(f"Amazon SP‑API call failed: {e}", "error")
        return False

def etsy_update_listing(item: dict, config: dict) -> bool:
    """Update an Etsy listing via Etsy API v3."""
    etsy_cfg = config.get("etsy", {})
    access_token = etsy_cfg.get("access_token")
    shop_id = etsy_cfg.get("shop_id")
    listing_id = item.get("etsy_listing_id")

    if not all([access_token, shop_id, listing_id]):
        return False

    url = f"https://openapi.etsy.com/v3/application/shops/{shop_id}/listings/{listing_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    data = {
        "title": item.get("title", ""),
        "description": item.get("description", ""),
        "price": float(item.get("price", 0.0)),
        "quantity": int(item.get("quantity", 0)),
        "who_made": "i_did",
        "when_made": "made_to_order"
    }
    try:
        resp = requests.patch(url, json=data, headers=headers, timeout=10)
        if resp.status_code == 200:
            return True
        else:
            _post(f"Etsy update failed for listing {listing_id}: {resp.text[:200]}", "warning")
            return False
    except Exception as e:
        _post(f"Etsy API error: {e}", "error")
        return False

# ── Sync engine ──────────────────────────────────────────────────
def sync_inventory(config: dict, state: dict):
    inventory_file = config.get("inventory_file", "inventory.json")
    if not os.path.exists(inventory_file):
        _post(f"Inventory file {inventory_file} not found", "warning")
        return

    try:
        with open(inventory_file, "r") as f:
            items = json.load(f)
    except Exception as e:
        _post(f"Failed to read inventory: {e}", "error")
        return

    marketplace_cfg = config.get("marketplaces", {})
    updated = 0
    for item in items:
        sku = item.get("sku")
        if not sku:
            continue
        fingerprint = item_fingerprint(item)
        prev_fp = state.get("fingerprints", {}).get(sku)
        if fingerprint == prev_fp:
            continue  # no change

        # Push to each marketplace if identifiers present
        results = []
        if marketplace_cfg.get("ebay") and item.get("ebay_item_id"):
            if ebay_update_listing(item, marketplace_cfg):
                results.append("ebay")

        if marketplace_cfg.get("amazon") and item.get("amazon_sku"):
            if amazon_update_listing(item, marketplace_cfg):
                results.append("amazon")

        if marketplace_cfg.get("etsy") and item.get("etsy_listing_id"):
            if etsy_update_listing(item, marketplace_cfg):
                results.append("etsy")

        if results:
            state.setdefault("fingerprints", {})[sku] = fingerprint
            _post(f"Synced SKU {sku} to: {', '.join(results)}", "info",
                  {"sku": sku, "marketplaces": results})
            updated += 1

    if updated > 0:
        _post(f"Total {updated} items updated", "info")
    save_state(config.get("state_file", "listing_sync_state.json"), state)

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Cross‑Platform Listing Sync Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "listing_sync_state.json")
        state = load_state(state_file)
        interval = int(config.get("poll_interval_seconds", 300))

        sync_inventory(config, state)

        _heartbeat()
        time.sleep(interval)

if __name__ == "__main__":
    main()
