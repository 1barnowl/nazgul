#!/usr/bin/env python3
"""
prompt_optimizer_bot.py — Prompt Optimizer Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
A/B tests different LLM prompts using real engagement
metrics collected from other bots. Periodically evaluates
which variant performs best and writes the winner to a
configuration file used by outreach/summarizer bots.

Attachable to the Nazgul BotController.

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `prompt_optimizer_config.json` in the same directory:

{
  "database_path": "prompt_metrics.db",
  "http_port": 9277,
  "poll_interval": 300,
  "lookback_minutes": 1440,
  "output_file": "active_prompt.json",
  "output_key": "active_prompt_id",
  "variants": [
    {
      "id": "v1_short",
      "text": "Hello {name}, quick update ..."
    },
    {
      "id": "v2_detailed",
      "text": "Hi {name}, I wanted to share some insights ..."
    }
  ]
}

Other bots submit metrics to:
  POST http://localhost:9277/metric
  {
    "prompt_id": "v1_short",
    "impressions": 50,
    "engagements": 12,
    "timestamp": "2025-01-15T14:30:00Z"   // optional, defaults to now
  }
"""

import json
import os
import sqlite3
import time
import threading
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import requests

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "prompt_optimizer_bot"
BOT_NAME = "Prompt Optimizer"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "prompt_optimizer_config.json"
CONFIG_PATH = Path(__file__).with_name(CONFIG_NAME)
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(CONFIG_NAME)

# ── Hub helpers ──────────────────────────────────────────────────────────────
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

# ── Database ─────────────────────────────────────────────────────────────────
def init_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prompt_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt_id TEXT NOT NULL,
            impressions INTEGER NOT NULL,
            engagements INTEGER NOT NULL,
            timestamp TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def insert_metric(db_path: str, entry: dict) -> None:
    timestamp = entry.get("timestamp") or datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO prompt_metrics (prompt_id, impressions, engagements, timestamp) VALUES (?,?,?,?)",
        (entry["prompt_id"], entry["impressions"], entry["engagements"], timestamp)
    )
    conn.commit()
    conn.close()

def get_aggregated_metrics(db_path: str, since: datetime) -> dict[str, dict]:
    """Return {prompt_id: {total_impressions, total_engagements, rate}}"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.execute(
        "SELECT prompt_id, SUM(impressions) as total_impressions, SUM(engagements) as total_engagements "
        "FROM prompt_metrics WHERE timestamp >= ? GROUP BY prompt_id",
        (since.isoformat(),)
    )
    rows = cursor.fetchall()
    conn.close()
    results = {}
    for row in rows:
        prompt_id = row["prompt_id"]
        impressions = row["total_impressions"] or 0
        engagements = row["total_engagements"] or 0
        rate = engagements / impressions if impressions > 0 else 0.0
        results[prompt_id] = {
            "impressions": impressions,
            "engagements": engagements,
            "engagement_rate": round(rate, 4)
        }
    return results

# ── HTTP API (for metrics ingestion) ─────────────────────────────────────────
class MetricsHandler(BaseHTTPRequestHandler):
    db_path: str = "prompt_metrics.db"

    def do_POST(self):
        if self.path == "/metric":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length)
                data = json.loads(body)
                required = ["prompt_id", "impressions", "engagements"]
                if not all(k in data for k in required):
                    self._respond(400, {"error": "Missing required fields (prompt_id, impressions, engagements)"})
                    return
                insert_metric(self.db_path, data)
                self._respond(200, {"status": "ok"})
            except Exception as e:
                self._respond(400, {"error": str(e)})
        else:
            self._respond(404, {})

    def _respond(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, *args):
        pass

def start_http_api(db_path: str, port: int) -> None:
    MetricsHandler.db_path = db_path
    server = HTTPServer(("0.0.0.0", port), MetricsHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"Metrics HTTP API listening on port {port}", "info")

# ── Winner selection ─────────────────────────────────────────────────────────
def select_winner(aggregated: dict[str, dict]) -> str | None:
    """Choose the prompt with the highest engagement rate. If there's a tie, pick the one with more impressions."""
    if not aggregated:
        return None
    # Sort by engagement_rate desc, then impressions desc
    sorted_prompts = sorted(aggregated.items(),
                            key=lambda x: (x[1]["engagement_rate"], x[1]["impressions"]),
                            reverse=True)
    return sorted_prompts[0][0]

def update_output_file(output_path: str, output_key: str, winner_id: str, variants: list[dict]) -> None:
    """Write a JSON file containing the winning prompt's configuration."""
    # Find the full prompt text and details
    winner = next((v for v in variants if v["id"] == winner_id), None)
    if not winner:
        _post(f"Winner {winner_id} not found in variants list", "warning")
        return
    # Write a dict like: {"active_prompt_id": "v2_detailed", "prompt_text": "..."}
    output = {
        output_key: winner_id,
        "prompt_text": winner["text"],
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    try:
        with open(output_path, "w") as f:
            json.dump(output, f, indent=2)
        _post(f"Deployed winner prompt {winner_id} to {output_path}", "info")
    except Exception as e:
        _post(f"Failed to write output file: {e}", "error")

# ── Main loop ────────────────────────────────────────────────────────────────
def main():
    _post("Prompt Optimizer Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        db_path = config.get("database_path", "prompt_metrics.db")
        http_port = int(config.get("http_port", 9277))
        poll_interval = int(config.get("poll_interval", 300))
        lookback_min = int(config.get("lookback_minutes", 1440))
        output_file = config.get("output_file", "active_prompt.json")
        output_key = config.get("output_key", "active_prompt_id")
        variants = config.get("variants", [])

        init_db(db_path)
        start_http_api(db_path, http_port)

        while True:
            since = datetime.now(timezone.utc) - timedelta(minutes=lookback_min)
            metrics = get_aggregated_metrics(db_path, since)

            if not metrics:
                _post("No engagement data in lookback window", "info")
            else:
                winner = select_winner(metrics)
                if winner:
                    update_output_file(output_file, output_key, winner, variants)
                    details = metrics.get(winner, {})
                    _post(f"Winner: {winner} (rate {details.get('engagement_rate',0):.4f})",
                          "info", {"winner": winner, "metrics": metrics})
                else:
                    _post("Could not determine winner", "warning")

            _heartbeat()
            time.sleep(poll_interval)

if __name__ == "__main__":
    main()
