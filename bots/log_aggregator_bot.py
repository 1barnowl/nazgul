#!/usr/bin/env python3
"""
log_aggregator_bot.py — Log Aggregator Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Ships structured logs from the BotController's SQLite
database to a central Loki or Elasticsearch instance.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `log_aggregator_config.json` in the same directory:

{
  "db_path": "/path/to/botcontroller.db",
  "target": "loki",
  "loki": {
    "url": "http://loki:3100/loki/api/v1/push",
    "labels": {
      "job": "botcontroller_logs"
    },
    "username": null,
    "password": null
  },
  "elasticsearch": {
    "url": "https://localhost:9200",
    "index": "bot-logs",
    "username": "elastic",
    "password": "changeme",
    "timeout_sec": 10
  },
  "poll_interval": 10,
  "batch_size": 200,
  "state_file": "log_aggregator_state.json"
}
"""

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

HUB      = "http://localhost:8765"
BOT_ID   = "log_aggregator_bot"
BOT_NAME = "Log Aggregator"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "log_aggregator_config.json"
CONFIG_PATH = Path(__file__).with_name(CONFIG_NAME)
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(CONFIG_NAME)

STATE_FILE = "log_aggregator_state.json"

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

# ── State persistence ────────────────────────────────────────────────────────
def load_state(state_path: str) -> int:
    """Load last processed message ID, default to 0."""
    try:
        with open(state_path, "r") as f:
            data = json.load(f)
            return data.get("last_id", 0)
    except Exception:
        return 0

def save_state(state_path: str, last_id: int) -> None:
    with open(state_path, "w") as f:
        json.dump({"last_id": last_id, "updated": datetime.now(timezone.utc).isoformat()}, f)

# ── DB fetch ─────────────────────────────────────────────────────────────────
def fetch_messages_since(db_path: str, since_id: int, limit: int) -> list[dict]:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT id, bot_id, bot_name, ts, summary, level, payload FROM messages WHERE id > ? ORDER BY id LIMIT ?",
            (since_id, limit)
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        _post(f"DB read error: {e}", "warning")
        return []

# ── Loki sender ──────────────────────────────────────────────────────────────
def ship_to_loki(messages: list[dict], config: dict) -> bool:
    loki_cfg = config.get("loki", {})
    url = loki_cfg.get("url")
    if not url:
        _post("Loki URL missing", "error")
        return False

    # Loki expects a JSON body with "streams" array
    streams = {}
    labels = loki_cfg.get("labels", {})
    label_key = json.dumps(labels, sort_keys=True)  # use as dict key
    streams[label_key] = []

    for m in messages:
        # Convert to nanosecond epoch
        ts = m.get("ts") or m.get("timestamp")
        try:
            dt = datetime.fromisoformat(ts)
        except Exception:
            dt = datetime.now(timezone.utc)
        ns = str(int(dt.timestamp() * 1e9))

        # Compose log line: summary + optional payload
        line = m.get("summary", "")
        extra = {}
        if m.get("payload"):
            try:
                extra = json.loads(m["payload"])
            except Exception:
                extra = {"raw_payload": m["payload"]}
        log_obj = {
            "bot_id": m["bot_id"],
            "bot_name": m["bot_name"],
            "level": m.get("level", "info"),
            "summary": line,
            **extra
        }
        streams[label_key].append([
            ns,
            json.dumps(log_obj)
        ])

    payload = {"streams": []}
    for lbl_set, entries in streams.items():
        payload["streams"].append({
            "stream": json.loads(lbl_set),
            "values": entries
        })

    auth = None
    if loki_cfg.get("username") and loki_cfg.get("password"):
        auth = (loki_cfg["username"], loki_cfg["password"])

    try:
        resp = requests.post(url, json=payload, auth=auth, timeout=15)
        if resp.status_code in (204, 200):
            return True
        else:
            _post(f"Loki push failed: HTTP {resp.status_code} {resp.text[:200]}", "error")
            return False
    except requests.RequestException as e:
        _post(f"Loki request error: {e}", "error")
        return False

# ── Elasticsearch sender ─────────────────────────────────────────────────────
def ship_to_elasticsearch(messages: list[dict], config: dict) -> bool:
    es_cfg = config.get("elasticsearch", {})
    url = es_cfg.get("url")
    if not url:
        _post("Elasticsearch URL missing", "error")
        return False
    index = es_cfg.get("index", "bot-logs")
    auth = None
    if es_cfg.get("username") and es_cfg.get("password"):
        auth = (es_cfg["username"], es_cfg["password"])

    # Build bulk request body
    bulk_body = ""
    for m in messages:
        ts = m.get("ts") or datetime.now(timezone.utc).isoformat()
        doc = {
            "bot_id": m["bot_id"],
            "bot_name": m["bot_name"],
            "summary": m["summary"],
            "level": m.get("level", "info"),
            "payload": m.get("payload"),
            "@timestamp": ts
        }
        action = json.dumps({"index": {"_index": index}})
        bulk_body += action + "\n" + json.dumps(doc) + "\n"

    headers = {"Content-Type": "application/x-ndjson"}
    try:
        resp = requests.post(
            f"{url.rstrip('/')}/_bulk",
            data=bulk_body,
            headers=headers,
            auth=auth,
            timeout=es_cfg.get("timeout_sec", 10)
        )
        if resp.ok and not resp.json().get("errors"):
            return True
        else:
            _post(f"Elasticsearch bulk failed: {resp.text[:300]}", "error")
            return False
    except requests.RequestException as e:
        _post(f"Elasticsearch request error: {e}", "error")
        return False

# ── Main loop ────────────────────────────────────────────────────────────────
def main():
    _post("Log Aggregator Bot online")

    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        db_path = config.get("db_path")
        if not db_path or not os.path.exists(db_path):
            _post(f"Database not found: {db_path}", "error")
            time.sleep(30)
            continue

        target = config.get("target", "loki")
        poll_interval = int(config.get("poll_interval", 10))
        batch_size = int(config.get("batch_size", 200))
        state_file = config.get("state_file", STATE_FILE)

        last_id = load_state(state_file)

        while True:
            messages = fetch_messages_since(db_path, last_id, batch_size)
            if messages:
                success = False
                if target == "loki":
                    success = ship_to_loki(messages, config)
                elif target == "elasticsearch":
                    success = ship_to_elasticsearch(messages, config)
                else:
                    _post(f"Unknown target: {target}", "error")
                    break

                if success:
                    new_last = max(m["id"] for m in messages)
                    last_id = new_last
                    save_state(state_file, last_id)
                    _post(f"Shipped {len(messages)} logs up to id {new_last}", "info")
                else:
                    _post(f"Shipment failed, will retry from id {last_id}", "warning")
            _heartbeat()
            time.sleep(poll_interval)

if __name__ == "__main__":
    main()
