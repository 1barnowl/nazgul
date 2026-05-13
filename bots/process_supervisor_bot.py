#!/usr/bin/env python3
"""
process_supervisor_bot.py — Process Supervisor Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors other bot processes (launched by the BotController or
by this supervisor itself), restarts crashed bots, and enforces
resource limits (CPU / memory) where possible.

Attachable to the Nazgul BotController – reports all actions
via the Hub (http://localhost:8765).

Configuration
─────────────
Place a file named `supervisor_config.json` in the same directory
as this script. It must contain a JSON list of bots to monitor:

[
  {
    "bot_id":  "momentum_chaser_bot",
    "script": "/path/to/momentum_chaser_bot.py",
    "args":   [],
    "cpu_max": 50.0,          # % CPU average over 30 s (0 = no limit)
    "mem_max_mb": 500.0,      # max RSS in MB (0 = no limit)
    "restart_window": 300,    # seconds
    "max_restarts": 5
  }
]

All values are optional except "bot_id" and "script".
If omitted, resource limits are disabled.

Requirements
────────────
    pip install psutil requests
"""

import json
import os
import signal
import subprocess
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import psutil
import requests

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "process_supervisor_bot"
BOT_NAME = "Process Supervisor"

# ── Intervals ─────────────────────────────────────────────────────────────────
SCAN_INTERVAL      = 10    # seconds between full supervision cycles
HEARTBEAT_INTERVAL = 20
COOLDOWN_MIN       = 5     # seconds to wait before restarting a crashed bot

# ── Configuration ─────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).with_name("supervisor_config.json")

if not CONFIG_PATH.exists():
    # Fallback: look in current working directory
    CONFIG_PATH = Path("supervisor_config.json")

_last_hb = 0.0

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

# ── Python interpreter discovery ──────────────────────────────────────────────
def _system_python() -> str:
    """Return the real Python interpreter (not PyInstaller bundle)."""
    if getattr(sys, "frozen", False):
        for name in ("python3", "python", "python3.12", "python3.11",
                     "python3.10", "python3.9"):
            exe = shutil.which(name)
            if exe:
                return exe
        return "python3"
    return sys.executable

# ── Process matching ──────────────────────────────────────────────────────────
def find_bot_process(bot_id: str, script_path: str) -> psutil.Process | None:
    """
    Locate a running process that matches the bot script file path.
    We search for 'python' processes whose command line contains the script name.
    """
    script_name = Path(script_path).name
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline = proc.info["cmdline"]
            if not cmdline:
                continue
            # The first arg is often the interpreter, second is script.
            # Accept any position if the script name appears as a command argument.
            if any(script_name == os.path.basename(arg) for arg in cmdline):
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None

# ── Resource limiting helpers ─────────────────────────────────────────────────
def apply_cpu_limit(pid: int, cpu_max: float) -> None:
    """
    Attempt to limit CPU usage using Linux cgroups v1/v2.
    This function is purely informational; actual enforcement requires a
    proper cgroup hierarchy set up by the administrator.
    On other platforms, we only report a warning.
    """
    if sys.platform != "linux":
        return
    # Placeholder for a real implementation: create/update a cgroup etc.
    # Not implemented here to keep dependencies minimal.
    # The bot will just log a warning via the Hub instead.
    _post(f"CPU limit of {cpu_max}% requested for PID {pid}, but cgroup setup required.",
          "warning", {"pid": pid})

def apply_mem_limit(pid: int, mem_mb: float) -> None:
    """
    Attempt to limit memory via cgroups.
    Same caveat as apply_cpu_limit.
    """
    if sys.platform != "linux":
        return
    _post(f"Memory limit of {mem_mb} MB requested for PID {pid}, but cgroup setup required.",
          "warning", {"pid": pid})

# ── Supervision logic ─────────────────────────────────────────────────────────
_supervised: dict = {}          # bot_id -> { "config": dict, "proc": Popen | None, "restart_count": int, "first_restart_ts": float }
_restart_history: dict = defaultdict(lambda: {"count": 0, "window_start": 0.0})

def _start_bot(cfg: dict) -> subprocess.Popen | None:
    try:
        python = _system_python()
        args = cfg.get("args", [])
        cmd = [python, cfg["script"]] + args
        proc = subprocess.Popen(
            cmd,
            cwd=os.path.dirname(cfg["script"]),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        # Give it a moment to start
        time.sleep(2)
        if proc.poll() is not None:
            out, _ = proc.communicate()
            _post(f"Bot {cfg['bot_id']} (script {cfg['script']}) failed to start immediately. Output: {out[:500]}",
                  "error")
            return None
        _post(f"Bot {cfg['bot_id']} started successfully (PID {proc.pid})", "info")
        return proc
    except Exception as e:
        _post(f"Could not start bot {cfg['bot_id']}: {e}", "error")
        return None

def _restart_bot(bot_id: str) -> subprocess.Popen | None:
    cfg = _supervised[bot_id]["config"]
    # Rate limit restarts
    now = time.time()
    hist = _restart_history[bot_id]
    window = cfg.get("restart_window", 300)
    max_r = cfg.get("max_restarts", 5)

    if hist["window_start"] <= 0 or now - hist["window_start"] > window:
        hist["count"] = 0
        hist["window_start"] = now

    if hist["count"] >= max_r:
        _post(f"Bot {bot_id}: maximum restarts ({max_r}) in {window}s reached. Will not restart again until window resets.",
              "warning")
        return None

    _post(f"Bot {bot_id} crashed. Restarting (attempt {hist['count']+1}/{max_r} in this window).")
    hist["count"] += 1
    return _start_bot(cfg)

def _resource_check(proc: psutil.Process, cfg: dict) -> None:
    """Check CPU and memory usage against configured limits, and issue alerts."""
    try:
        cpu_pct = proc.cpu_percent(interval=1)
    except psutil.NoSuchProcess:
        return
    except Exception:
        cpu_pct = 0.0

    cpu_max = cfg.get("cpu_max", 0)
    if cpu_max > 0 and cpu_pct > cpu_max:
        _post(f"Bot {cfg['bot_id']} (PID {proc.pid}) CPU: {cpu_pct:.1f}% > limit {cpu_max}%",
              "warning",
              {"pid": proc.pid, "cpu_percent": cpu_pct, "limit": cpu_max})
        apply_cpu_limit(proc.pid, cpu_max)

    mem_max_mb = cfg.get("mem_max_mb", 0)
    if mem_max_mb > 0:
        try:
            rss_mb = proc.memory_info().rss / (1024 * 1024)
        except psutil.NoSuchProcess:
            return
        except Exception:
            rss_mb = 0.0
        if rss_mb > mem_max_mb:
            _post(f"Bot {cfg['bot_id']} (PID {proc.pid}) Memory: {rss_mb:.1f} MB > limit {mem_max_mb} MB",
                  "warning",
                  {"pid": proc.pid, "rss_mb": rss_mb, "limit": mem_max_mb})
            apply_mem_limit(proc.pid, mem_max_mb)

def _supervision_cycle() -> None:
    try:
        with open(CONFIG_PATH, "r") as f:
            config_list = json.load(f)
    except Exception as e:
        _post(f"Failed to load supervisor config {CONFIG_PATH}: {e}", "error")
        return

    # Build set of known bot_ids
    current_bot_ids = {cfg["bot_id"] for cfg in config_list}

    # Remove supervised entries that are no longer in config
    for bid in list(_supervised.keys()):
        if bid not in current_bot_ids:
            proc = _supervised.pop(bid)
            if proc and proc.get("proc") and proc["proc"].poll() is None:
                try:
                    proc["proc"].terminate()
                except Exception:
                    pass
            _post(f"Bot {bid} removed from supervision config.", "info")

    for cfg in config_list:
        bid = cfg["bot_id"]
        if bid not in _supervised:
            # New entry – attempt to locate existing process or start one
            proc = find_bot_process(bid, cfg["script"])
            if proc is not None:
                _supervised[bid] = {"config": cfg, "proc": None}   # already running, we don't own the process
                _post(f"Bot {bid} already running (PID {proc.pid}) – taking over monitoring.", "info")
            else:
                _supervised[bid] = {"config": cfg, "proc": None}
                new_proc = _start_bot(cfg)
                if new_proc:
                    _supervised[bid]["proc"] = new_proc
        else:
            # Already supervised
            entry = _supervised[bid]
            proc = entry.get("proc")

            if proc is not None:
                # We own the process – check if it's alive
                ret = proc.poll()
                if ret is not None:
                    _post(f"Bot {bid} exited with code {ret}.", "warning")
                    entry["proc"] = _restart_bot(bid)
                else:
                    # Alive: resource check
                    try:
                        ps_proc = psutil.Process(proc.pid)
                        _resource_check(ps_proc, cfg)
                    except psutil.NoSuchProcess:
                        # Rare: process disappeared between poll and here
                        entry["proc"] = _restart_bot(bid)
                    except Exception:
                        pass
            else:
                # We don't own the process (was already running when supervisor started)
                # Verify it's still alive by pid lookup
                ps_proc = find_bot_process(bid, cfg["script"])
                if ps_proc is None:
                    _post(f"Bot {bid} (externally launched) appears to have stopped. Restarting.", "warning")
                    entry["proc"] = _start_bot(cfg)
                else:
                    _resource_check(ps_proc, cfg)

        _heartbeat()

def main() -> None:
    _post("Process Supervisor Bot online. Monitoring bots per supervisor_config.json", "info")
    while True:
        try:
            _supervision_cycle()
        except Exception as e:
            _post(f"Unexpected error in supervision cycle: {e}\n{traceback.format_exc()}", "error")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()
