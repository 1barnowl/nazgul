#!/usr/bin/env python3
"""
latency_profiler_bot.py — Latency Profiler Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Samples execution times of configured commands to detect
performance regressions in the system or codebase.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `latency_profiler_config.json` in the same directory:

{
  "profile_commands": [
    {
      "name": "test_import",
      "command": "python3",
      "args": ["-c", "import yfinance, numpy, sklearn"],
      "timeout": 15,
      "threshold_factor": 1.5,
      "min_samples": 5
    },
    {
      "name": "rest_api_smoke",
      "command": "curl",
      "args": ["-s", "-o", "/dev/null", "-w", "%{http_code}", "https://httpbin.org/get"],
      "timeout": 10,
      "threshold_factor": 2.0,
      "min_samples": 10
    }
  ],
  "state_file": "latency_profiler_state.json",
  "sample_interval_seconds": 600
}
"""

import json
import os
import statistics
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

HUB      = "http://localhost:8765"
BOT_ID   = "latency_profiler_bot"
BOT_NAME = "Latency Profiler"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "latency_profiler_config.json"
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

# ── State persistence ────────────────────────────────────────────────────────
def load_state(state_path: str) -> dict:
    try:
        with open(state_path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_state(state_path: str, state: dict) -> None:
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Profiling function ───────────────────────────────────────────────────────
def profile_command(cmd: list[str], timeout: int) -> float | None:
    """Run command and return elapsed wall-clock seconds, or None on failure."""
    try:
        start = time.perf_counter()
        result = subprocess.run(
            cmd,
            timeout=timeout,
            capture_output=True,
            text=True
        )
        elapsed = time.perf_counter() - start
        if result.returncode != 0:
            return None  # failure (stderr can be logged elsewhere)
        return elapsed
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None

# ── Anomaly detection ────────────────────────────────────────────────────────
def check_regression(history: list[float], threshold_factor: float,
                     min_samples: int) -> dict | None:
    """If latest value deviates from baseline, return alert dict, else None."""
    if len(history) < min_samples:
        return None
    mean = statistics.mean(history[:-1])  # all except latest
    std = statistics.stdev(history[:-1]) if len(history[:-1]) >= 2 else 0.0
    latest = history[-1]
    if std == 0.0:
        # If all previous times identical, any increase is suspicious
        if latest > mean * 1.1:
            std = 0.001  # avoid zero division
        else:
            return None
    deviation = (latest - mean) / std
    if deviation > threshold_factor:
        return {
            "mean_s": round(mean, 4),
            "std_s": round(std, 4),
            "latest_s": round(latest, 4),
            "deviation": round(deviation, 2),
            "threshold_factor": threshold_factor
        }
    return None

# ── Main loop ────────────────────────────────────────────────────────────────
def main():
    _post("Latency Profiler Bot online")

    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "latency_profiler_state.json")
        sample_interval = int(config.get("sample_interval_seconds", 600))
        commands = config.get("profile_commands", [])

        state = load_state(state_file)

        for cmd_cfg in commands:
            name = cmd_cfg["name"]
            command = cmd_cfg["command"]
            args = cmd_cfg.get("args", [])
            timeout = cmd_cfg.get("timeout", 30)
            factor = float(cmd_cfg.get("threshold_factor", 2.0))
            min_samples = int(cmd_cfg.get("min_samples", 10))

            full_cmd = [command] + args
            elapsed = profile_command(full_cmd, timeout)

            if elapsed is None:
                _post(f"{name}: failed to run or timed out", "warning",
                      {"name": name, "command": full_cmd})
                continue

            # Update history
            if name not in state:
                state[name] = []
            state[name].append(elapsed)
            # Keep a sane buffer
            if len(state[name]) > 500:
                state[name] = state[name][-500:]

            regression_alert = check_regression(state[name], factor, min_samples)
            if regression_alert:
                _post(
                    f"{name}: performance regression detected! "
                    f"Latest: {elapsed:.3f}s, mean: {regression_alert['mean_s']:.3f}s, "
                    f"dev: {regression_alert['deviation']}x",
                    "error",
                    {"name": name, "stats": regression_alert}
                )
            else:
                _post(f"{name}: {elapsed:.3f}s (normal)", "info",
                      {"name": name, "elapsed_s": elapsed})

        save_state(state_file, state)
        _heartbeat()
        time.sleep(sample_interval)

if __name__ == "__main__":
    main()
