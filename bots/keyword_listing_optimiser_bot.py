#!/usr/bin/env python3
"""
keyword_listing_optimiser_bot.py — Keyword & Listing Optimisation Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Analyses search trends via Google Trends and/or eBay Finding API,
then rewrites listing titles/descriptions using an LLM (OpenAI) to
boost discoverability and conversion. Can operate passively (post
suggestions to the Hub) or actively (update eBay listings).

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install pytrends requests

Configuration
─────────────
Place `keyword_optimiser_config.json` in the same directory:

{
  "keywords_to_track": ["wireless headphones", "mechanical keyboard"],
  "trends": {
    "provider": "google",            // "google" (pytrends) or "ebay"
    "google": {
      "geo": "US",
      "timeframe": "today 3-m"
    },
    "ebay": {
      "app_id": "YOUR_EBAY_APP_ID",
      "search_terms": ["wireless headphones"]
    }
  },
  "llm": {
    "provider": "openai",
    "api_key": "sk-...",
    "model": "gpt-4o-mini",
    "temperature": 0.3,
    "max_tokens": 500,
    "endpoint": null
  },
  "listings": {
    "source": "file",               // "file" or "ebay_api"
    "file": "my_listings.json",
    "ebay_api": {
      "client_id": "...",
      "client_secret": "...",
      "refresh_token": "...",
      "sandbox": true
    }
  },
  "active": false,                   // if true, actually update listings on eBay
  "http_port": 9700,
  "state_file": "keyword_optimiser_state.json",
  "heartbeat_interval": 30,
  "poll_interval_hours": 24
}

Listings file format (`my_listings.json`):
[
  {
    "item_id": "123456789012",
    "title": "Wireless Bluetooth Headphones",
    "description": "High quality sound...",
    "ebay_item_id": "123456789012"  // if using active mode
  },
  ...
]
"""

import json
import os
import time
import re
import threading
from collections import Counter
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Dict, List, Optional, Set

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "keyword_listing_optimiser_bot"
BOT_NAME = "Keyword & Listing Optimiser"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "keyword_optimiser_config.json"
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

# ── Stop words (simple list for keyword extraction) ─────────────
STOP_WORDS = {"a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
              "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
              "being", "have", "has", "had", "do", "does", "did", "will", "would",
              "could", "should", "may", "might", "can", "shall", "you", "i", "my",
              "your", "he", "she", "it", "they", "we", "them", "us", "our", "its",
              "this", "that", "these", "those", "not", "no", "nor", "as", "so",
              "if", "then", "else", "when", "where", "why", "how", "all", "each",
              "every", "both", "few", "more", "most", "other", "some", "such",
              "only", "own", "same", "than", "too", "very", "just"}

def extract_significant_words(text: str) -> List[str]:
    """Extract meaningful words from a search query, ignoring short/stop words."""
    words = re.findall(r'[a-zA-Z0-9]+', text.lower())
    return [w for w in words if w not in STOP_WORDS and len(w) > 2]

# ── Trend providers ─────────────────────────────────────────────

def google_trends_keywords(config: dict) -> List[str]:
    """Return a list of trending keywords related to the tracked terms."""
    try:
        from pytrends.request import TrendReq
    except ImportError:
        _post("pytrends not installed; pip install pytrends", "error")
        return []

    keywords = config.get("keywords_to_track", [])
    if not keywords:
        return []

    google_cfg = config.get("trends", {}).get("google", {})
    geo = google_cfg.get("geo", "US")
    timeframe = google_cfg.get("timeframe", "today 3-m")

    try:
        pytrends = TrendReq(hl='en-US', tz=360)
        trending_words = set()
        for kw in keywords:
            pytrends.build_payload([kw], cat=0, timeframe=timeframe, geo=geo, gprop='')
            related = pytrends.related_queries()
            if kw in related and related[kw]['rising'] is not None:
                for row in related[kw]['rising'].itertuples():
                    words = extract_significant_words(row.Index)
                    for w in words:
                        trending_words.add(w)
            time.sleep(1.5)  # avoid rate limits
        return list(trending_words)
    except Exception as e:
        _post(f"Google Trends error: {e}", "warning")
        return []

def ebay_trends_keywords(config: dict) -> List[str]:
    """Use eBay Finding API to collect frequent title words from recent sold items."""
    ebay_cfg = config.get("trends", {}).get("ebay", {})
    app_id = ebay_cfg.get("app_id")
    if not app_id:
        return []
    search_terms = ebay_cfg.get("search_terms", config.get("keywords_to_track", []))
    if not search_terms:
        return []

    base_url = "https://svcs.ebay.com/services/search/FindingService/v1"
    word_counter = Counter()
    try:
        for term in search_terms:
            params = {
                "OPERATION-NAME": "findCompletedItems",
                "SERVICE-VERSION": "1.0.0",
                "SECURITY-APPNAME": app_id,
                "RESPONSE-DATA-FORMAT": "JSON",
                "REST-PAYLOAD": "",
                "keywords": term[:350],
                "itemFilter(0).name": "SoldItemsOnly",
                "itemFilter(0).value": "true",
                "paginationInput.entriesPerPage": "10"
            }
            resp = requests.get(base_url, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("findCompletedItemsResponse", [{}])[0].get("searchResult", {}).get("item", [])
                for item in items:
                    title = item.get("title", "")
                    for word in extract_significant_words(title):
                        word_counter[word] += 1
            time.sleep(1.2)
        # Return top 20 most frequent words as trending keywords
        return [word for word, cnt in word_counter.most_common(20)]
    except Exception as e:
        _post(f"eBay trend fetch error: {e}", "warning")
        return []

def get_trending_keywords(config: dict) -> List[str]:
    provider = config.get("trends", {}).get("provider", "google")
    if provider == "google":
        return google_trends_keywords(config)
    elif provider == "ebay":
        return ebay_trends_keywords(config)
    else:
        _post(f"Unknown trend provider: {provider}", "error")
        return []

# ── LLM rewriting ────────────────────────────────────────────────
def call_llm(prompt: str, config: dict) -> Optional[str]:
    llm_cfg = config.get("llm", {})
    if not llm_cfg.get("api_key"):
        return None
    endpoint = llm_cfg.get("endpoint") or "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {llm_cfg['api_key']}", "Content-Type": "application/json"}
    data = {
        "model": llm_cfg.get("model", "gpt-4o-mini"),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": float(llm_cfg.get("temperature", 0.3)),
        "max_tokens": int(llm_cfg.get("max_tokens", 500)),
    }
    try:
        resp = requests.post(endpoint, json=data, headers=headers, timeout=20)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
        else:
            _post(f"LLM error: {resp.status_code} {resp.text[:200]}", "warning")
            return None
    except Exception as e:
        _post(f"LLM call failed: {e}", "warning")
        return None

def rewrite_listing(title: str, description: str, trending_keywords: List[str], llm_config: dict) -> Optional[dict]:
    """Return new_title and new_description from LLM."""
    kw_str = ", ".join(trending_keywords[:10])
    prompt = (
        f"You are an expert e‑commerce copywriter. Rewrite the following product listing title and description "
        f"to naturally incorporate trending keywords where relevant. Do not stuff keywords; improve readability "
        f"and conversion. Return a JSON object with keys 'new_title' and 'new_description'.\n\n"
        f"Trending keywords: {kw_str}\n\n"
        f"Original title: {title}\n"
        f"Original description: {description}\n\n"
        f"Return only the JSON object."
    )
    response = call_llm(prompt, llm_config)
    if not response:
        return None
    # Try to parse JSON from response (may include markdown fences)
    try:
        cleaned = response.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            lines = [l for l in lines if not l.startswith("```")]
            cleaned = "\n".join(lines)
        return json.loads(cleaned)
    except Exception as e:
        _post(f"Failed to parse LLM response: {e}", "warning")
        return None

# ── eBay active update (optional) ───────────────────────────────
def ebay_update_listing(item_id: str, new_title: str, new_desc: str, config: dict) -> bool:
    """Use eBay Trading API (ReviseFixedPriceItem) to update a listing."""
    ebay_cfg = config.get("listings", {}).get("ebay_api", {})
    client_id = ebay_cfg.get("client_id")
    client_secret = ebay_cfg.get("client_secret")
    refresh_token = ebay_cfg.get("refresh_token")
    sandbox = ebay_cfg.get("sandbox", True)

    if not all([client_id, client_secret, refresh_token]):
        _post("eBay credentials not configured for active update", "error")
        return False

    from ebay_oauth.token import OAuthToken
    token_obj = OAuthToken(client_id, client_secret)
    try:
        access_token_data = token_obj.getAccessToken(None, None, refresh_token)
        access_token = access_token_data["access_token"]
    except Exception as e:
        _post(f"eBay OAuth error: {e}", "error")
        return False

    endpoint = "https://api.sandbox.ebay.com/ws/api.dll" if sandbox else "https://api.ebay.com/ws/api.dll"
    headers = {
        "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
        "X-EBAY-API-CALL-NAME": "ReviseFixedPriceItem",
        "X-EBAY-API-IAF-TOKEN": access_token,
        "Content-Type": "text/xml"
    }
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<ReviseFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <Item>
    <ItemID>{item_id}</ItemID>
    <Title>{new_title[:80]}</Title>
    <Description><![CDATA[{new_desc[:5000]}]]></Description>
  </Item>
</ReviseFixedPriceItemRequest>"""
    try:
        resp = requests.post(endpoint, data=body, headers=headers, timeout=15)
        if resp.status_code == 200 and "<Ack>Success</Ack>" in resp.text:
            return True
        else:
            _post(f"eBay revise failed for {item_id}: {resp.text[:200]}", "warning")
            return False
    except Exception as e:
        _post(f"eBay API error: {e}", "error")
        return False

# ── Listing source loader ───────────────────────────────────────
def load_listings(config: dict) -> List[dict]:
    src_cfg = config.get("listings", {})
    source_type = src_cfg.get("source", "file")
    if source_type == "file":
        path = src_cfg.get("file", "my_listings.json")
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    return json.load(f)
            except Exception as e:
                _post(f"Error loading listings file: {e}", "warning")
        return []
    elif source_type == "ebay_api":
        # Could fetch all seller items via Trading API (GetMyeBaySelling) – not implemented in this slim version
        _post("eBay listing fetch not implemented yet; using empty list", "warning")
        return []
    else:
        return []

# ── HTTP API for on-demand optimisation ─────────────────────────
class OptimiserHandler(BaseHTTPRequestHandler):
    config: dict = {}

    def do_POST(self):
        if self.path == "/optimise":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                title = data.get("title", "")
                description = data.get("description", "")
                if not title:
                    self._respond(400, {"error": "Missing title"})
                    return
                trending_kw = get_trending_keywords(self.config)
                if not trending_kw:
                    self._respond(500, {"error": "No trending keywords found"})
                    return
                result = rewrite_listing(title, description, trending_kw, self.config)
                if not result:
                    self._respond(500, {"error": "LLM rewriting failed"})
                    return
                summary = f"Optimised title: {result.get('new_title', '')[:80]}"
                _post(summary, "info", {"original_title": title, "new_title": result.get("new_title")})
                self._respond(200, {"new_title": result.get("new_title"), "new_description": result.get("new_description")})
            except Exception as e:
                self._respond(500, {"error": str(e)})
        else:
            self._respond(404, {})

    def _respond(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, *args):
        pass

def start_http(port: int, config: dict):
    OptimiserHandler.config = config
    server = HTTPServer(("0.0.0.0", port), OptimiserHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"Keyword Optimiser API on port {port}", "info")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Keyword & Listing Optimisation Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        # Start HTTP API
        port = int(config.get("http_port", 9700))
        start_http(port, config)

        active = config.get("active", False)
        poll_hours = int(config.get("poll_interval_hours", 24))
        state_file = config.get("state_file", "keyword_optimiser_state.json")
        try:
            with open(state_file, "r") as f:
                state = json.load(f)
        except Exception:
            state = {"last_optimised_ids": []}

        # Main cycle
        while True:
            # 1. Fetch trending keywords
            trending_kw = get_trending_keywords(config)
            if not trending_kw:
                _post("No trending keywords available; skipping optimisation", "warning")
                _heartbeat()
                time.sleep(poll_hours * 3600)
                continue

            _post(f"Trending keywords: {', '.join(trending_kw[:15])}", "info")

            # 2. Load listings
            listings = load_listings(config)
            if not listings:
                _post("No listings loaded", "info")

            # 3. Process each listing
            for listing in listings:
                item_id = listing.get("ebay_item_id") or listing.get("item_id", "")
                if item_id in state.get("last_optimised_ids", []):
                    continue

                title = listing.get("title", "")
                description = listing.get("description", "")
                if not title:
                    continue

                rewritten = rewrite_listing(title, description, trending_kw, config)
                if not rewritten:
                    continue

                new_title = rewritten.get("new_title")
                new_desc = rewritten.get("new_description")
                if not new_title:
                    continue

                payload = {
                    "item_id": item_id,
                    "original_title": title,
                    "optimised_title": new_title,
                    "optimised_description": new_desc
                }
                _post(f"Optimised listing {item_id}: {new_title[:80]}", "info", payload)

                if active and item_id:
                    if ebay_update_listing(item_id, new_title, new_desc, config):
                        _post(f"Active update: eBay listing {item_id} revised", "info")
                    else:
                        _post(f"Active update failed for {item_id}", "warning")

                # Mark as optimised in state
                state.setdefault("last_optimised_ids", []).append(item_id)

            # Trim state list
            state["last_optimised_ids"] = state["last_optimised_ids"][-200:]
            with open(state_file, "w") as f:
                json.dump(state, f, indent=2)

            _heartbeat()
            time.sleep(poll_hours * 3600)

if __name__ == "__main__":
    main()
