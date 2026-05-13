#!/usr/bin/env python3
"""
anomaly_detection_bot.py — Anomaly Detection Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Uses an Isolation Forest model to detect unusual spikes
in bot message activity (potential runaway loops) by
reading the BotController's SQLite DB directly.

Configuration
─────────────
Place `anomaly_config.json` in the same directory:

{
  "db_path": "/path/to/botcontroller.db",
  "poll_interval": 30,
  "window_minutes": 10,
  "history_size": 500,
  "retrain_interval": 50,
  "contamination": 0.05,
  "anomaly_threshold": -0.1,
  "bots_ignore": ["process_supervisor_bot"]
}

Requirements
────────────
    pip install scikit-learn requests
"""

import json
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import requests
from sklearn.ensemble import IsolationForest

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "anomaly_detection_bot"
BOT_NAME = "Anomaly Detection Bot"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

# ── Configuration path ────────────────────────────────────────────────────────
CONFIG_NAME = "anomaly_config.json"
CONFIG_PATH = Path(__file__).with_name(CONFIG_NAME)
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(CONFIG_NAME)

# ── Hub helpers ────────────────────────────────────────────────────────────────
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

# ── DB query ──────────────────────────────────────────────────────────────────
def fetch_recent_messages(db_path: str, since: datetime,
                          bots_ignore: set[str]) -> list[dict]:
    """Return messages after 'since' for all bots except ignored ones."""
    try:
        # Open read-only to avoid locking issues
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT bot_id, ts, level FROM messages WHERE ts >= ? ORDER BY ts",
            (since.isoformat(),)
        )
        rows = cursor.fetchall()
        conn.close()
        # Filter by ignored bots
        return [dict(r) for r in rows if r["bot_id"] not in bots_ignore]
    except Exception as e:
        _post(f"Failed to fetch from DB: {e}", "warning")
        return []

# ── Feature extraction ────────────────────────────────────────────────────────
def extract_features(messages: list[dict], window_minutes: int) -> dict[str, list[float]]:
    """
    For each bot, compute:
    - total message count in the window
    - error count (level == 'error')
    - time span between first and last message (seconds)
    Returns a dict: bot_id -> [count, error_count, spread_s]
    """
    bots = defaultdict(list)
    for m in messages:
        bots[m["bot_id"]].append(m)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=window_minutes)
    features = {}
    for bid, msgs in bots.items():
        # Filter messages within the exact window (already done if since = cutoff, but double-check)
        valid = [m for m in msgs if datetime.fromisoformat(m["ts"]) >= cutoff]
        if not valid:
            continue
        count = len(valid)
        errors = sum(1 for m in valid if m["level"] == "error")
        # Time spread: difference between earliest and latest in seconds
        timestamps = sorted([datetime.fromisoformat(m["ts"]) for m in valid])
        spread_s = (timestamps[-1] - timestamps[0]).total_seconds() if count > 1 else 0.0
        features[bid] = [float(count), float(errors), spread_s]
    return features

# ── Anomaly detector ──────────────────────────────────────────────────────────
class AnomalyDetector:
    def __init__(self, contamination=0.05, history_size=500):
        self.model = None
        self.contamination = contamination
        self.history_data = []   # list of feature rows
        self.history_size = history_size
        self.retrain_counter = 0

    def add_and_predict(self, feature_vector: list[float]) -> float:
        """
        Returns anomaly score (negative = anomalous).
        Only if model is trained; else returns 0.
        """
        if not self.model:
            return 0.0
        # Predict expects 2D array
        X = np.array([feature_vector])
        return self.model.decision_function(X)[0]

    def update_history(self, features_dict: dict[str, list[float]]):
        for bot_id, feat in features_dict.items():
            self.history_data.append(feat)
            # Limit history size
            if len(self.history_data) > self.history_size:
                self.history_data = self.history_data[-self.history_size:]

    def maybe_retrain(self, retrain_interval: int):
        self.retrain_counter += 1
        if self.retrain_counter >= retrain_interval and len(self.history_data) >= 10:
            try:
                X = np.array(self.history_data)
                self.model = IsolationForest(
                    contamination=self.contamination,
                    random_state=42,
                    n_estimators=100
                )
                self.model.fit(X)
                self.retrain_counter = 0
                _post(f"Model retrained with {len(self.history_data)} samples", "info")
            except Exception as e:
                _post(f"Model training failed: {e}", "warning")

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _post("Anomaly Detection Bot online")
    try:
        with open(CONFIG_PATH, "r") as f:
            config = json.load(f)
    except Exception as e:
        _post(f"Failed to load config: {e}", "error")
        return

    db_path = config.get("db_path")
    poll_interval = int(config.get("poll_interval", 30))
    window_minutes = int(config.get("window_minutes", 10))
    history_size = int(config.get("history_size", 500))
    retrain_interval = int(config.get("retrain_interval", 50))
    contamination = float(config.get("contamination", 0.05))
    anomaly_threshold = float(config.get("anomaly_threshold", -0.1))
    ignore_bots = set(config.get("bots_ignore", []))

    detector = AnomalyDetector(contamination=contamination, history_size=history_size)

    while True:
        now = datetime.now(timezone.utc)
        since = now - timedelta(minutes=window_minutes)
        messages = fetch_recent_messages(db_path, since, ignore_bots)
        if not messages:
            _heartbeat()
            time.sleep(poll_interval)
            continue

        features = extract_features(messages, window_minutes)
        if not features:
            _heartbeat()
            time.sleep(poll_interval)
            continue

        # Update history with current features (for training)
        detector.update_history(features)
        # Retrain periodically
        detector.maybe_retrain(retrain_interval)

        # Score each bot
        for bot_id, feat in features.items():
            score = detector.add_and_predict(feat)
            payload = {
                "bot_id": bot_id,
                "features": {
                    "message_count": feat[0],
                    "error_count": feat[1],
                    "time_spread_s": feat[2]
                },
                "anomaly_score": float(score)
            }
            if score < anomaly_threshold:
                _post(f"Anomaly detected: {bot_id} (score {score:.3f}) -> possible runaway or excessive activity",
                      "error", payload)
            else:
                # optionally log normal
                _post(f"Bot {bot_id} ok ({feat[0]:.0f} msgs, score {score:.3f})",
                      "info", payload)

        _heartbeat()
        time.sleep(poll_interval)

if __name__ == "__main__":
    main()
