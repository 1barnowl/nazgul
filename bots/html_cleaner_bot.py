#!/usr/bin/env python3
"""
html_cleaner_bot.py — HTML Cleaner Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Strips boilerplate, ads, and navigation from scraped
HTML using readability algorithms and exposes a simple
HTTP API for other bots.

Attachable to the Nazgul BotController.

Requirements
────────────
    pip install readability-lxml justext beautifulsoup4 requests

Configuration
─────────────
Place `html_cleaner_config.json` in the same directory:

{
  "http_port": 9505,
  "mode": "http",
  "file_watch": {
    "enabled": false,
    "directory": "/data/raw_html",
    "output_directory": "/data/clean_text"
  },
  "heartbeat_interval": 30
}
"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "html_cleaner_bot"
BOT_NAME = "HTML Cleaner"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "html_cleaner_config.json"
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

# ── HTML cleaning logic ──────────────────────────────────────────
def clean_html(raw_html: str, url: str = "") -> str:
    """Extract main content from HTML using readability + fallbacks."""
    # Remove scripts, styles, and other non-content tags first
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(raw_html, "lxml")
        for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
            tag.decompose()
        cleaned = str(soup)
    except ImportError:
        cleaned = raw_html  # fallback without BS

    # Try readability-lxml
    try:
        from readability import Document
        doc = Document(cleaned, url=url)
        text = doc.summary()
        # Remove any remaining HTML tags
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(text, "lxml")
        result = soup.get_text(separator="\n", strip=True)
        return result
    except ImportError:
        pass
    except Exception:
        # readability failed; try justext
        pass

    # Fallback: justext (language-agnostic)
    try:
        import justext
        paragraphs = justext.justext(cleaned, justext.get_stoplist("English"))
        cleaned_text = "\n".join(p.text for p in paragraphs if not p.is_boilerplate)
        return cleaned_text
    except ImportError:
        pass
    except Exception:
        pass

    # Final fallback: use BeautifulSoup to strip tags
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(raw_html, "lxml")
        return soup.get_text(separator="\n", strip=True)
    except Exception:
        return raw_html  # last resort

# ── HTTP API handler ─────────────────────────────────────────────
class CleanHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/clean":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                raw_html = data.get("html", "")
                url = data.get("url", "")
                if not raw_html:
                    self._respond(400, {"error": "Missing 'html' field"})
                    return
                cleaned = clean_html(raw_html, url)
                summary = f"Cleaned HTML ({len(raw_html)} bytes) -> text ({len(cleaned)} chars)"
                _post(summary, "info", {"input_size": len(raw_html), "output_size": len(cleaned)})
                self._respond(200, {"text": cleaned})
            except Exception as e:
                self._respond(400, {"error": str(e)})
        else:
            self._respond(404, {"error": "Not found"})

    def _respond(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, *args):
        pass

def start_http_server(port: int):
    server = HTTPServer(("0.0.0.0", port), CleanHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"HTML Cleaner HTTP API started on port {port}", "info")

# ── File watcher (optional) ─────────────────────────────────────
def watch_directory(directory: str, output_dir: str, processed_cache: set):
    """Check for new HTML files in directory and process them."""
    try:
        entries = os.listdir(directory)
    except FileNotFoundError:
        return
    for filename in entries:
        if not filename.endswith((".html", ".htm")):
            continue
        filepath = os.path.join(directory, filename)
        if filepath in processed_cache:
            continue
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                raw = f.read()
            cleaned = clean_html(raw, url="")
            out_name = Path(filename).stem + ".txt"
            out_path = os.path.join(output_dir, out_name)
            with open(out_path, "w", encoding="utf-8") as out:
                out.write(cleaned)
            _post(f"Processed {filename} -> {out_name}", "info", {"file": filename})
            processed_cache.add(filepath)
        except Exception as e:
            _post(f"Error processing {filename}: {e}", "warning")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("HTML Cleaner Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        port = int(config.get("http_port", 9505))
        mode = config.get("mode", "http")
        file_cfg = config.get("file_watch", {})

        if mode in ("http", "both"):
            start_http_server(port)

        if mode in ("file", "both") and file_cfg.get("enabled"):
            directory = file_cfg.get("directory")
            output_dir = file_cfg.get("output_directory")
            if directory and output_dir:
                os.makedirs(output_dir, exist_ok=True)
                processed_cache = set()
                # Run file watcher in a loop
                while True:
                    watch_directory(directory, output_dir, processed_cache)
                    _heartbeat()
                    time.sleep(10)

        # Heartbeat loop for HTTP-only mode
        while True:
            _heartbeat()
            time.sleep(10)

if __name__ == "__main__":
    main()
