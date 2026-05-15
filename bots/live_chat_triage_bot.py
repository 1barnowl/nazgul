#!/usr/bin/env python3
"""
live_chat_triage_bot.py — Live‑Chat Escalation & Triage Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Routes live chat conversations to the right human agent based
on intent and urgency. Chat messages can be submitted via HTTP
or from a file directory. Triage results are posted to the
Nazgul BotController and optionally forwarded to agent groups
via callbacks.

Attachable to the BotController dashboard (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `live_chat_triage_config.json` in the same directory:

{
  "triage": {
    "intent_keywords": {
      "sales":     ["buy", "price", "purchase", "demo", "trial"],
      "support":   ["help", "error", "issue", "broken", "not working"],
      "complaint": ["cancel", "refund", "angry", "complaint", "manager"]
    },
    "urgency_keywords": {
      "high":   ["urgent", "asap", "immediately", "critical"],
      "medium": ["important", "soon"],
      "low":    ["whenever", "no rush"]
    }
  },
  "agent_groups": [
    {
      "name": "Sales Team",
      "match_intent": ["sales"],
      "match_urgency": ["high", "medium"],
      "priority": 1,
      "callback_url": "https://mycrm.example.com/api/assign?group=sales"
    },
    {
      "name": "Support Engineers",
      "match_intent": ["support"],
      "match_urgency": ["high", "medium", "low"],
      "priority": 2,
      "callback_url": null
    },
    {
      "name": "Retention Team",
      "match_intent": ["complaint"],
      "match_urgency": ["high"],
      "priority": 1,
      "callback_url": "https://mycrm.example.com/api/assign?group=retention"
    },
    {
      "name": "General Agents",
      "match_intent": [],
      "match_urgency": [],
      "priority": 99,
      "callback_url": null
    }
  ],
  "http_port": 9670,
  "file_watch": {
    "enabled": false,
    "directory": "/data/chat_queue",
    "output_directory": "/data/chat_triage"
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
from typing import List, Dict, Optional

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "live_chat_triage_bot"
BOT_NAME = "Live‑Chat Triage"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "live_chat_triage_config.json"
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

# ── Triage logic ─────────────────────────────────────────────────
class TriageEngine:
    def __init__(self, config: dict):
        self.intent_keywords = config.get("triage", {}).get("intent_keywords", {})
        self.urgency_keywords = config.get("triage", {}).get("urgency_keywords", {})
        self.agent_groups = config.get("agent_groups", [])

    def detect_intent(self, text: str) -> str:
        """Return highest confidence intent based on keyword count."""
        scores = {}
        text_lower = text.lower()
        for intent, kws in self.intent_keywords.items():
            score = sum(1 for kw in kws if kw in text_lower)
            if score > 0:
                scores[intent] = score
        if not scores:
            return "general"
        return max(scores, key=scores.get)

    def detect_urgency(self, text: str) -> str:
        """Return urgency level based on keyword count."""
        scores = {}
        text_lower = text.lower()
        for urgency, kws in self.urgency_keywords.items():
            score = sum(1 for kw in kws if kw in text_lower)
            if score > 0:
                scores[urgency] = score
        if not scores:
            return "medium"  # default
        return max(scores, key=scores.get)

    def assign_agent(self, intent: str, urgency: str) -> dict:
        """Select the best matching agent group."""
        best = None
        best_priority = 999
        for group in self.agent_groups:
            match_intents = group.get("match_intent", [])
            match_urgencies = group.get("match_urgency", [])
            if (not match_intents or intent in match_intents) and \
               (not match_urgencies or urgency in match_urgencies):
                priority = group.get("priority", 99)
                if priority < best_priority:
                    best = group
                    best_priority = priority
        if not best:
            # fallback to last group with no match criteria (default)
            for group in self.agent_groups:
                if not group.get("match_intent") and not group.get("match_urgency"):
                    best = group
                    break
        return best if best else {"name": "Unassigned", "callback_url": None}

    def triage(self, message: str) -> Dict:
        intent = self.detect_intent(message)
        urgency = self.detect_urgency(message)
        agent = self.assign_agent(intent, urgency)
        return {
            "intent": intent,
            "urgency": urgency,
            "assigned_agent_group": agent["name"],
            "callback_url": agent.get("callback_url")
        }

# ── HTTP API handler ─────────────────────────────────────────────
class TriageHandler(BaseHTTPRequestHandler):
    engine: TriageEngine = None

    def do_POST(self):
        if self.path == "/triage":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                message = data.get("message", "")
                chat_id = data.get("chat_id", "unknown")
                customer = data.get("customer", "anonymous")
                if not message:
                    self._respond(400, {"error": "Missing 'message' field"})
                    return
                result = self.engine.triage(message)
                # Build summary
                summary = f"Chat {chat_id} from {customer}: intent={result['intent']}, urgency={result['urgency']} → {result['assigned_agent_group']}"
                payload = {
                    "chat_id": chat_id,
                    "customer": customer,
                    "message": message[:200],
                    "intent": result["intent"],
                    "urgency": result["urgency"],
                    "assigned_agent": result["assigned_agent_group"]
                }
                # Post to Hub
                _post(summary, "info", payload)
                # Optional callback to external system
                callback_url = result.get("callback_url")
                if callback_url:
                    try:
                        requests.post(callback_url, json=payload, timeout=5)
                    except Exception as e:
                        _post(f"Agent callback failed for {callback_url}: {e}", "warning")
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

def start_http(port: int, engine: TriageEngine):
    TriageHandler.engine = engine
    server = HTTPServer(("0.0.0.0", port), TriageHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"Live Chat Triage API on port {port}", "info")

# ── File watcher (optional) ─────────────────────────────────────
def watch_directory(directory: str, output_dir: str, processed: set, engine: TriageEngine):
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
            with open(fpath, "r") as f:
                data = json.load(f)
            message = data.get("message", "")
            if not message:
                _post(f"File {fname} missing 'message'", "warning")
                processed.add(fpath)
                continue
            result = engine.triage(message)
            result["chat_id"] = data.get("chat_id", fname)
            result["customer"] = data.get("customer", "unknown")
            summary = f"Chat {result['chat_id']}: intent={result['intent']}, urgency={result['urgency']} → {result['assigned_agent_group']}"
            _post(summary, "info", result)
            # Write output
            out_name = Path(fname).stem + "_triage.json"
            out_path = os.path.join(output_dir, out_name)
            with open(out_path, "w") as out:
                json.dump(result, out, indent=2)
            processed.add(fpath)
        except Exception as e:
            _post(f"Error processing {fname}: {e}", "warning")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Live Chat Triage Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        engine = TriageEngine(config)
        port = int(config.get("http_port", 9670))
        start_http(port, engine)

        file_cfg = config.get("file_watch", {})
        if file_cfg.get("enabled"):
            directory = file_cfg.get("directory")
            output_dir = file_cfg.get("output_directory")
            if directory and output_dir:
                os.makedirs(output_dir, exist_ok=True)
                processed_cache = set()
                while True:
                    watch_directory(directory, output_dir, processed_cache, engine)
                    _heartbeat()
                    time.sleep(10)
        else:
            while True:
                _heartbeat()
                time.sleep(10)

if __name__ == "__main__":
    main()
