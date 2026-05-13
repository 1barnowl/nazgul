#!/usr/bin/env python3
"""
configuration_drift_detector_bot.py — Configuration Drift Detector Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Compares live system state against a declarative YAML configuration and
alerts on any unauthorised changes (drift). Designed for Linux.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install pyyaml psutil requests

Configuration
─────────────
Place `drift_config.yml` in the same directory as this script:

scan_interval: 600  # seconds between full checks

resources:
  files:
    - path: /etc/nginx/nginx.conf
      sha256: "abcdef1234..."
      mode: "0644"
      owner: root
      group: root
  processes:
    - name: nginx
      count: 1          # expected number of running instances
  tcp_ports:
    - port: 80
      expected_state: LISTEN
"""

import hashlib
import json
import os
import pwd
import grp
import time
from pathlib import Path

import psutil
import requests
import yaml

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "config_drift_detector_bot"
BOT_NAME = "Configuration Drift Detector"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

# ── Configuration path ────────────────────────────────────────────────────────
CONFIG_NAME = "drift_config.yml"
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

# ── File checks ───────────────────────────────────────────────────────────────
def check_files(declared_files: list) -> list[dict]:
    drifts = []
    for item in declared_files:
        path = item.get("path")
        if not path:
            continue
        actual = {}
        try:
            stat = os.stat(path)
            # File type – must be a regular file
            if not os.path.isfile(path):
                drifts.append({
                    "resource": "file",
                    "path": path,
                    "drift": "not_a_regular_file",
                    "expected": "regular file",
                    "actual": "other"
                })
                continue

            # Mode
            mode = oct(stat.st_mode)[-4:]  # e.g. '0644'
            expected_mode = item.get("mode")
            if expected_mode and mode != expected_mode:
                actual["mode"] = mode
                drifts.append({
                    "resource": "file",
                    "path": path,
                    "attribute": "mode",
                    "expected": expected_mode,
                    "actual": mode
                })

            # Owner
            expected_owner = item.get("owner")
            if expected_owner:
                try:
                    owner_name = pwd.getpwuid(stat.st_uid).pw_name
                except KeyError:
                    owner_name = str(stat.st_uid)
                if owner_name != expected_owner:
                    drifts.append({
                        "resource": "file",
                        "path": path,
                        "attribute": "owner",
                        "expected": expected_owner,
                        "actual": owner_name
                    })

            # Group
            expected_group = item.get("group")
            if expected_group:
                try:
                    group_name = grp.getgrgid(stat.st_gid).gr_name
                except KeyError:
                    group_name = str(stat.st_gid)
                if group_name != expected_group:
                    drifts.append({
                        "resource": "file",
                        "path": path,
                        "attribute": "group",
                        "expected": expected_group,
                        "actual": group_name
                    })

            # SHA256
            expected_sha = item.get("sha256")
            if expected_sha:
                sha = hashlib.sha256()
                with open(path, "rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        sha.update(chunk)
                actual_sha = sha.hexdigest()
                if actual_sha != expected_sha:
                    drifts.append({
                        "resource": "file",
                        "path": path,
                        "attribute": "sha256",
                        "expected": expected_sha,
                        "actual": actual_sha
                    })

        except FileNotFoundError:
            drifts.append({
                "resource": "file",
                "path": path,
                "drift": "missing",
                "expected": "present",
                "actual": "absent"
            })
        except PermissionError as e:
            drifts.append({
                "resource": "file",
                "path": path,
                "drift": "permission_denied",
                "detail": str(e)
            })
        except Exception as e:
            drifts.append({
                "resource": "file",
                "path": path,
                "drift": "check_error",
                "detail": str(e)
            })
    return drifts

# ── Process checks ────────────────────────────────────────────────────────────
def check_processes(declared_procs: list) -> list[dict]:
    drifts = []
    # Build a map of expected counts per process name
    expected_map = {}
    for item in declared_procs:
        name = item.get("name")
        if not name:
            continue
        expected_map[name] = item.get("count", None)

    # Count actual running processes
    actual_counts = {}
    for proc in psutil.process_iter(["name"]):
        try:
            pname = proc.info["name"]
            actual_counts[pname] = actual_counts.get(pname, 0) + 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    for name, exp_count in expected_map.items():
        actual = actual_counts.get(name, 0)
        if exp_count is not None and actual != exp_count:
            drifts.append({
                "resource": "process",
                "name": name,
                "expected_count": exp_count,
                "actual_count": actual
            })
    return drifts

# ── TCP port checks ───────────────────────────────────────────────────────────
def check_tcp_ports(declared_ports: list) -> list[dict]:
    drifts = []
    for item in declared_ports:
        port = item.get("port")
        if not port:
            continue
        expected_state = item.get("expected_state", "LISTEN")
        # Find actual listening ports
        found = False
        for conn in psutil.net_connections(kind="tcp"):
            if conn.laddr.port == port and conn.status == expected_state:
                found = True
                break
        if not found:
            drifts.append({
                "resource": "tcp_port",
                "port": port,
                "expected_state": expected_state,
                "actual": "not found or wrong state"
            })
    return drifts

# ── Scan engine ───────────────────────────────────────────────────────────────
def run_drift_scan(config: dict):
    resources = config.get("resources", {})
    all_drifts = []

    file_spec = resources.get("files", [])
    if file_spec:
        all_drifts.extend(check_files(file_spec))

    proc_spec = resources.get("processes", [])
    if proc_spec:
        all_drifts.extend(check_processes(proc_spec))

    port_spec = resources.get("tcp_ports", [])
    if port_spec:
        all_drifts.extend(check_tcp_ports(port_spec))

    if all_drifts:
        for drift in all_drifts:
            # Build a summary line
            resource_type = drift.get("resource")
            if resource_type == "file":
                path = drift.get("path")
                if "missing" in drift.get("drift",""):
                    _post(f"File missing: {path}", "error", drift)
                elif "permission_denied" in drift.get("drift",""):
                    _post(f"Cannot check file: {path}", "warning", drift)
                else:
                    attr = drift.get("attribute","?")
                    _post(f"Drift in {path}: {attr} expected {drift.get('expected')} actual {drift.get('actual')}",
                          "error", drift)
            elif resource_type == "process":
                name = drift.get("name")
                _post(f"Process count drift: {name} expected {drift.get('expected_count')} actual {drift.get('actual_count')}",
                      "error", drift)
            elif resource_type == "tcp_port":
                port = drift.get("port")
                _post(f"Open port drift: port {port} not in state {drift.get('expected_state')}",
                      "error", drift)
            else:
                _post(f"Drift detected: {json.dumps(drift)}", "warning", drift)
    else:
        _post("No configuration drift detected", "info")

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _post("Configuration Drift Detector Bot online", "info")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = yaml.safe_load(f)
        except Exception as e:
            _post(f"Failed to load drift config: {e}", "error")
            time.sleep(60)
            continue

        scan_interval = int(config.get("scan_interval", 600))
        try:
            run_drift_scan(config)
        except Exception as e:
            _post(f"Unexpected error during drift scan: {e}", "error")

        _heartbeat()
        time.sleep(scan_interval)

if __name__ == "__main__":
    main()
