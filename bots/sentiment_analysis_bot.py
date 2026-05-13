#!/usr/bin/env python3
"""
sentiment_analysis_bot.py — Sentiment Analysis Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Scores text for positive/negative/urgent tone using a
HuggingFace sentiment model and keyword‑based urgency
detection. Exposes an HTTP API for other bots.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install transformers torch requests

Configuration
─────────────
Place `sentiment_config.json` in the same directory:

{
  "model_name": "distilbert-base-uncased-finetuned-sst-2-english",
  "urgency_keywords": ["urgent", "asap", "immediately", "critical", "emergency", "attention"],
  "http_port": 9565,
  "heartbeat_interval": 30,
  "file_watch": {
    "enabled": false,
    "directory": "/data/text_in",
    "output_directory": "/data/sentiment_out"
  }
}
"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import List, Dict, Optional

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "sentiment_analysis_bot"
BOT_NAME = "Sentiment Analyzer"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "sentiment_config.json"
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

# ── Sentiment / urgency logic ────────────────────────────────────
class SentimentAnalyzer:
    def __init__(self, config: dict):
        self._init_model(config.get("model_name", "distilbert-base-uncased-finetuned-sst-2-english"))
        self.urgency_keywords = [w.lower() for w in config.get("urgency_keywords", [])]

    def _init_model(self, model_name: str):
        from transformers import pipeline
        # Use the pipeline for sentiment analysis (returns POSITIVE/NEGATIVE with score)
        self.pipeline = pipeline("sentiment-analysis", model=model_name, return_all_scores=False)

    def analyze(self, text: str) -> dict:
        # Sentiment
        result = self.pipeline(text[:512])  # truncate to avoid token limit
        sentiment = result[0] if result else {"label": "NEUTRAL", "score": 0.0}

        # Urgency: simple keyword density score (0‑1)
        text_lower = text.lower()
        total_words = len(text_lower.split())
        if total_words == 0:
            urgency = 0.0
        else:
            count = sum(1 for kw in self.urgency_keywords if kw in text_lower)
            urgency = min(1.0, count / (len(self.urgency_keywords) * 0.5))  # heuristic

        return {
            "sentiment": sentiment,
            "urgency": round(urgency, 3)
        }

# ── Global analyzer instance ─────────────────────────────────────
analyzer: Optional[SentimentAnalyzer] = None

# ── HTTP API handler ─────────────────────────────────────────────
class SentimentHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/analyze":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                text = data.get("text", "")
                if not text:
                    self._respond(400, {"error": "Missing 'text' field"})
                    return
                result = analyzer.analyze(text)
                summary = f"Sentiment: {result['sentiment']['label']} (urg {result['urgency']:.2f})"
                _post(summary, "info", {"text_snippet": text[:100], "result": result})
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
    server = HTTPServer(("0.0.0.0", port), SentimentHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"Sentiment API listening on port {port}", "info")

# ── File watcher (optional) ─────────────────────────────────────
def watch_directory(directory: str, output_dir: str, processed: set):
    try:
        entries = os.listdir(directory)
    except FileNotFoundError:
        return
    for fname in entries:
        if not fname.endswith(".txt"):
            continue
        fpath = os.path.join(directory, fname)
        if fpath in processed:
            continue
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                text = f.read()
            result = analyzer.analyze(text)
            out_name = Path(fname).stem + ".json"
            out_path = os.path.join(output_dir, out_name)
            with open(out_path, "w", encoding="utf-8") as out:
                json.dump({"source": fname, "analysis": result}, out, indent=2)
            _post(f"Processed {fname} → {out_name}", "info", {"file": fname})
            processed.add(fpath)
        except Exception as e:
            _post(f"Error processing {fname}: {e}", "warning")

# ── Main loop ────────────────────────────────────────────────────
def main():
    global analyzer
    _post("Sentiment Analysis Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        # Initialize analyzer with new config
        analyzer = SentimentAnalyzer(config)
        port = int(config.get("http_port", 9565))
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
