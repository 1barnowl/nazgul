#!/usr/bin/env python3
"""
universal_web_scraper_bot.py — Universal Web Scraper Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Uses Playwright with auto‑rotating proxies and fingerprint
spoofing to extract data from any URL. Periodic and on‑demand.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install playwright playwright-stealth requests
    playwright install chromium   # (run once)

Configuration
─────────────
Place `scraper_config.json` in the same directory:

{
  "headless": true,
  "browser_timeout": 30,
  "proxy_manager_url": "http://localhost:9123/fingerprint/acquire",   // optional
  "proxies": [],   // static fallback if no proxy manager
  "tasks": [
    {
      "id": "example_page",
      "url": "https://example.com",
      "selectors": [
        {"field": "title", "selector": "h1"}
      ],
      "interval_minutes": 60
    }
  ],
  "http_api": {
    "port": 9233,
    "auth_token": null
  },
  "poll_interval": 60,
  "state_file": "scraper_state.json"
}
"""

import json
import os
import threading
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "universal_web_scraper_bot"
BOT_NAME = "Universal Web Scraper"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "scraper_config.json"
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
        return {}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Proxy helpers ─────────────────────────────────────────────────
def get_best_proxy(proxy_manager_url: str) -> str | None:
    """Get a single proxy URL from the proxy rotation manager."""
    try:
        resp = requests.get(proxy_manager_url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            # The proxy rotation bot returns a list of best proxies; pick first
            if isinstance(data, list) and len(data) > 0:
                return data[0].get("url") or data[0].get("proxy")
        return None
    except Exception:
        return None

# ── Playwright scraper ────────────────────────────────────────────
def scrape_page(task: dict, proxy: str | None, config: dict) -> dict | None:
    """Launch Playwright, apply stealth, extract selectors, return data."""
    try:
        from playwright.sync_api import sync_playwright
        import playwright_stealth
    except ImportError as e:
        _post(f"Playwright/stealth not installed: {e}", "error")
        return None

    headless = config.get("headless", True)
    timeout = config.get("browser_timeout", 30) * 1000  # milliseconds
    url = task["url"]
    selectors = task.get("selectors", [])

    try:
        with sync_playwright() as p:
            browser_args = []
            launch_kwargs = {
                "headless": headless,
                "args": browser_args
            }
            if proxy:
                # Proxy format: http://user:pass@host:port
                launch_kwargs["proxy"] = {"server": proxy}

            browser = p.chromium.launch(**launch_kwargs)
            context = browser.new_context()

            # Apply stealth (fingerprint spoofing)
            try:
                playwright_stealth.sync_stealth_sync(context)
            except Exception as e:
                _post(f"Stealth injection error: {e}", "warning")

            page = context.new_page()
            page.goto(url, timeout=timeout, wait_until="domcontentloaded")

            extracted = {}
            for item in selectors:
                field = item.get("field")
                sel = item.get("selector")
                if not field or not sel:
                    continue
                try:
                    elements = page.query_selector_all(sel)
                    if elements:
                        extracted[field] = [el.inner_text() for el in elements]
                    else:
                        extracted[field] = []
                except Exception as e:
                    extracted[field] = {"error": str(e)}

            browser.close()
            return {
                "url": url,
                "data": extracted,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }

    except Exception as e:
        _post(f"Scrape failed for {url}: {e}", "warning")
        return None

# ── HTTP API (on‑demand scraping) ─────────────────────────────────
from http.server import HTTPServer, BaseHTTPRequestHandler

class ScraperHandler(BaseHTTPRequestHandler):
    config: dict = None
    tasks: list = None
    pending: list = None  # list to append new tasks (thread-safe)

    def do_POST(self):
        if self.path == "/scrape":
            auth = self.config.get("http_api", {}).get("auth_token")
            if auth and self.headers.get("Authorization") != f"Bearer {auth}":
                self._respond(401, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                task = json.loads(body)
                if not task.get("url"):
                    self._respond(400, {"error": "url required"})
                    return
                task_id = task.get("id") or f"on_demand_{int(time.time())}"
                # Add to pending list
                new_task = {
                    "id": task_id,
                    "url": task["url"],
                    "selectors": task.get("selectors", []),
                    "interval_minutes": 0  # one-shot
                }
                self.pending.append(new_task)
                self._respond(202, {"task_id": task_id, "status": "queued"})
            except Exception as e:
                self._respond(400, {"error": str(e)})
        else:
            self._respond(404, {})

    def do_GET(self):
        self._respond(405, {})

    def _respond(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, *args):
        pass

def start_api(config: dict, pending_list: list):
    api_cfg = config.get("http_api", {})
    if not api_cfg.get("port"):
        return
    port = int(api_cfg["port"])
    ScraperHandler.config = config
    ScraperHandler.pending = pending_list
    server = HTTPServer(("0.0.0.0", port), ScraperHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"Scraper HTTP API started on port {port}", "info")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Universal Web Scraper Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "scraper_state.json")
        state = load_state(state_file)
        tasks = config.get("tasks", [])
        proxy_manager_url = config.get("proxy_manager_url")
        proxies_static = config.get("proxies", [])
        poll_interval = int(config.get("poll_interval", 60))

        # On-demand pending list (shared with HTTP API)
        pending_tasks = []
        start_api(config, pending_tasks)

        while True:
            now = datetime.now(timezone.utc)
            # Check periodic tasks
            for task in tasks:
                task_id = task["id"]
                last_run = state.get(task_id)
                if last_run:
                    last_dt = datetime.fromisoformat(last_run)
                    interval = timedelta(minutes=task.get("interval_minutes", 60))
                    if now - last_dt < interval:
                        continue
                # Acquire proxy
                proxy_url = None
                if proxy_manager_url:
                    proxy_url = get_best_proxy(proxy_manager_url)
                if not proxy_url and proxies_static:
                    proxy_url = proxies_static[0]  # simplest rotation; could be enhanced

                result = scrape_page(task, proxy_url, config)
                if result:
                    summary = f"Scraped {task['url']}: extracted fields {list(result['data'].keys())}"
                    _post(summary, "info", {"task_id": task_id, "result": result})
                else:
                    _post(f"Scrape failed for task {task_id}", "warning")

                state[task_id] = now.isoformat()
                save_state(state_file, state)

            # Process any on-demand tasks (pop from pending)
            while pending_tasks:
                task = pending_tasks.pop(0)
                proxy_url = None
                if proxy_manager_url:
                    proxy_url = get_best_proxy(proxy_manager_url)
                if not proxy_url and proxies_static:
                    proxy_url = proxies_static[0]

                result = scrape_page(task, proxy_url, config)
                if result:
                    _post(f"On‑demand scrape {task['url']} complete", "info", {"task_id": task["id"], "result": result})
                else:
                    _post(f"On‑demand scrape failed for {task['url']}", "warning")

            _heartbeat()
            time.sleep(poll_interval)

if __name__ == "__main__":
    main()
