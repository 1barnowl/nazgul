#!/usr/bin/env python3
"""
product_matcher_bot.py — Product Matcher Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Matches scraped product titles to canonical GTIN,
ASIN, or SKU identifiers using a local database
(SQLite + fuzzy matching) or the free UPCitemdb API.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install rapidfuzz requests

Configuration
─────────────
Place `product_matcher_config.json` in the same directory:

{
  "engine": "sqlite",                // "sqlite" or "upcitemdb"
  "sqlite": {
    "db_path": "products.db",
    "table": "products",
    "title_column": "title",
    "sku_column": "sku",
    "gtin_column": "gtin",
    "asin_column": "asin",
    "fuzzy_threshold": 85           // 0-100, higher = stricter match
  },
  "upcitemdb": {
    "api_key": null,                 // optional for higher rate limit
    "search_url": "https://api.upcitemdb.com/prod/trial/lookup",
    "rate_limit_delay": 1.2          // seconds between requests (free tier)
  },
  "http_port": 9575,
  "file_watch": {
    "enabled": false,
    "directory": "/data/product_titles",
    "output_directory": "/data/matched_products"
  },
  "heartbeat_interval": 30
}
"""

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional, Dict, List, Tuple

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "product_matcher_bot"
BOT_NAME = "Product Matcher"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "product_matcher_config.json"
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

# ── Product Matcher engines ──────────────────────────────────────

class BaseMatcher:
    def match(self, title: str, attributes: dict = None) -> Dict:
        raise NotImplementedError

class SQLiteMatcher(BaseMatcher):
    def __init__(self, config: dict):
        self.db_path = config.get("db_path", "products.db")
        self.table = config.get("table", "products")
        self.title_col = config.get("title_column", "title")
        self.sku_col = config.get("sku_column", "sku")
        self.gtin_col = config.get("gtin_column", "gtin")
        self.asin_col = config.get("asin_column", "asin")
        self.threshold = int(config.get("fuzzy_threshold", 85))

    def match(self, title: str, attributes: dict = None) -> Dict:
        from rapidfuzz import process, fuzz
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            cursor = conn.execute(f"SELECT {self.title_col}, {self.sku_col}, {self.gtin_col}, {self.asin_col} FROM {self.table}")
            rows = cursor.fetchall()
            conn.close()
        except Exception as e:
            return {"error": f"Database error: {e}"}

        if not rows:
            return {"error": "No products in database"}

        titles = [row[0] for row in rows]
        best_match = process.extractOne(title, titles, scorer=fuzz.token_sort_ratio, score_cutoff=self.threshold)
        if not best_match:
            return {"matched": False, "confidence": 0, "message": "No match above threshold"}

        match_title, confidence, index = best_match
        matched_row = rows[index]
        result = {
            "matched": True,
            "confidence": confidence,
            "matched_title": match_title,
            "sku": matched_row[1] if len(matched_row) > 1 else None,
            "gtin": matched_row[2] if len(matched_row) > 2 else None,
            "asin": matched_row[3] if len(matched_row) > 3 else None,
            "source": "sqlite"
        }
        return result

class UPCitemdbMatcher(BaseMatcher):
    def __init__(self, config: dict):
        self.api_key = config.get("api_key")
        self.search_url = config.get("search_url", "https://api.upcitemdb.com/prod/trial/lookup")
        self.rate_limit = float(config.get("rate_limit_delay", 1.2))
        self.last_request = 0.0

    def match(self, title: str, attributes: dict = None) -> Dict:
        # Respect rate limit
        elapsed = time.time() - self.last_request
        if elapsed < self.rate_limit:
            time.sleep(self.rate_limit - elapsed)
        self.last_request = time.time()

        # UPCitemdb accepts keyword search (free tier)
        params = {"s": title}
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            resp = requests.get(self.search_url, params=params, headers=headers, timeout=10)
            data = resp.json()
            if data.get("code") != "OK" or not data.get("items"):
                return {"matched": False, "confidence": 0, "message": data.get("message", "No results")}
            # Take the first item as best match
            item = data["items"][0]
            result = {
                "matched": True,
                "confidence": 100 if item.get("title") == title else 80,  # heuristic
                "matched_title": item.get("title", ""),
                "sku": item.get("offers", [{}])[0].get("sku") if item.get("offers") else None,
                "gtin": item.get("upc") or item.get("ean"),
                "asin": item.get("asin"),
                "source": "upcitemdb"
            }
            return result
        except Exception as e:
            return {"error": f"UPCitemdb API error: {e}"}

# ── Global engine ────────────────────────────────────────────────
matcher_engine: Optional[BaseMatcher] = None

def init_matcher(config: dict):
    global matcher_engine
    engine_name = config.get("engine", "sqlite")
    if engine_name == "sqlite":
        matcher_engine = SQLiteMatcher(config.get("sqlite", {}))
    elif engine_name == "upcitemdb":
        matcher_engine = UPCitemdbMatcher(config.get("upcitemdb", {}))
    else:
        raise ValueError(f"Unsupported engine: {engine_name}")

# ── HTTP API handler ─────────────────────────────────────────────
class MatcherHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/match":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                title = data.get("title", "")
                if not title:
                    self._respond(400, {"error": "Missing 'title' field"})
                    return
                attributes = data.get("attributes")
                result = matcher_engine.match(title, attributes)
                summary = f"Matched '{title[:50]}...' -> {result.get('matched_title', 'no match')}"
                _post(summary, "info", {"title": title, "result": result})
                self._respond(200, result)
            except Exception as e:
                self._respond(500, {"error": str(e)})
        else:
            self._respond(404, {"error": "Not found"})

    def _respond(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, *args):
        pass

def start_http(port: int):
    server = HTTPServer(("0.0.0.0", port), MatcherHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"Product Matcher API listening on port {port}", "info")

# ── File watcher (optional) ─────────────────────────────────────
def watch_directory(directory: str, output_dir: str, processed: set):
    try:
        entries = os.listdir(directory)
    except FileNotFoundError:
        return
    for fname in entries:
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(directory, fname)
        if fpath in processed:
            continue
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            title = data.get("title") or data.get("product_title")
            if not title:
                _post(f"File {fname} missing title field", "warning")
                processed.add(fpath)
                continue
            result = matcher_engine.match(title, data.get("attributes"))
            out_name = Path(fname).stem + "_matched.json"
            out_path = os.path.join(output_dir, out_name)
            with open(out_path, "w", encoding="utf-8") as out:
                json.dump({"query": title, "match": result}, out, indent=2)
            _post(f"Matched {fname} → {out_name}", "info", {"file": fname})
            processed.add(fpath)
        except Exception as e:
            _post(f"Error processing {fname}: {e}", "warning")

# ── Main loop ────────────────────────────────────────────────────
def main():
    global matcher_engine
    _post("Product Matcher Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        init_matcher(config)
        port = int(config.get("http_port", 9575))
        start_http(port)

        file_cfg = config.get("file_watch", {})
        if file_cfg.get("enabled"):
            directory = file_cfg.get("directory")
            output_dir = file_cfg.get("output_directory")
            if directory and output_dir:
                os.makedirs(output_dir, exist_ok=True)
                processed_cache = set()
                while True:
                    watch_directory(directory, output_dir, processed_cache)
                    _heartbeat()
                    time.sleep(10)
        else:
            while True:
                _heartbeat()
                time.sleep(10)

if __name__ == "__main__":
    main()
