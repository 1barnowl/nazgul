#!/usr/bin/env python3
"""
file_watcher_bot.py — File Watcher Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors local directories, SFTP servers, and S3 buckets
for new or updated CSV, JSON, or PDF files and reports them
to the Nazgul BotController.

Attachable to the BotController dashboard (http://localhost:8765).

Requirements
────────────
    pip install watchdog paramiko boto3 requests

Configuration
─────────────
Place `file_watcher_config.json` in the same directory:

{
  "watchers": [
    {
      "id": "local_invoices",
      "type": "local",
      "path": "/data/incoming",
      "extensions": ["csv", "json", "pdf"],
      "process_command": "python3 /scripts/invoice_processor.py {filepath}"
    },
    {
      "id": "s3_bucket_logs",
      "type": "s3",
      "bucket": "my-logs",
      "prefix": "ingest/",
      "extensions": ["csv", "json"],
      "aws_region": "us-east-1",
      "aws_access_key": "YOUR_ACCESS_KEY",
      "aws_secret_key": "YOUR_SECRET_KEY"
    },
    {
      "id": "sftp_drops",
      "type": "sftp",
      "host": "sftp.example.com",
      "port": 22,
      "username": "user",
      "password": "pass",
      "remote_path": "/uploads",
      "extensions": ["csv", "json"],
      "process_command": "curl -X POST -d @{filepath} http://processor:8080"
    }
  ],
  "poll_interval_seconds": 60,
  "state_file": "file_watcher_state.json"
}
"""

import json
import os
import subprocess
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "file_watcher_bot"
BOT_NAME = "File Watcher"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "file_watcher_config.json"
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

# ── File detection for local directory ───────────────────────────
def get_local_files(directory: str, extensions: List[str]) -> Dict[str, dict]:
    """Return dict of filepath -> {size, mtime} for matching files."""
    results = {}
    try:
        for entry in os.scandir(directory):
            if entry.is_file():
                ext = entry.name.rsplit(".", 1)[-1].lower() if "." in entry.name else ""
                if not extensions or ext in extensions:
                    stat = entry.stat()
                    results[entry.path] = {
                        "size": stat.st_size,
                        "mtime": stat.st_mtime
                    }
    except FileNotFoundError:
        pass
    except Exception as e:
        _post(f"Error scanning {directory}: {e}", "warning")
    return results

# ── File detection for S3 bucket ─────────────────────────────────
def get_s3_files(bucket: str, prefix: str, extensions: List[str],
                 aws_config: dict) -> Dict[str, dict]:
    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        _post("boto3 not installed", "error")
        return {}
    try:
        s3 = boto3.client("s3",
                          region_name=aws_config.get("region", "us-east-1"),
                          aws_access_key_id=aws_config.get("aws_access_key"),
                          aws_secret_access_key=aws_config.get("aws_secret_key"))
        paginator = s3.get_paginator("list_objects_v2")
        results = {}
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
                if extensions and ext not in extensions:
                    continue
                results[key] = {
                    "size": obj["Size"],
                    "mtime": obj["LastModified"].timestamp(),
                    "etag": obj["ETag"].strip('"')
                }
        return results
    except Exception as e:
        _post(f"S3 error for {bucket}: {e}", "warning")
        return {}

# ── File detection for SFTP ─────────────────────────────────────
def get_sftp_files(host: str, port: int, username: str, password: str,
                   remote_path: str, extensions: List[str]) -> Dict[str, dict]:
    try:
        import paramiko
    except ImportError:
        _post("paramiko not installed", "error")
        return {}
    try:
        transport = paramiko.Transport((host, port))
        transport.connect(username=username, password=password)
        sftp = paramiko.SFTPClient.from_transport(transport)
        results = {}
        for entry in sftp.listdir_attr(remote_path):
            if not entry.filename.startswith("."):
                ext = entry.filename.rsplit(".", 1)[-1].lower() if "." in entry.filename else ""
                if extensions and ext not in extensions:
                    continue
                filepath = f"{remote_path}/{entry.filename}"
                results[filepath] = {
                    "size": entry.st_size,
                    "mtime": entry.st_mtime
                }
        sftp.close()
        transport.close()
        return results
    except Exception as e:
        _post(f"SFTP error for {host}: {e}", "warning")
        return {}

# ── Compare with previous state to detect new/changed files ─────
def detect_new_files(watcher_id: str, current_files: Dict[str, dict],
                     state: dict) -> List[str]:
    """Return list of filepaths that are new or have changed."""
    previous = state.get(watcher_id, {})
    new_or_changed = []
    for filepath, attrs in current_files.items():
        prev = previous.get(filepath)
        if not prev:
            new_or_changed.append(filepath)
        else:
            # Check size and mtime
            if (attrs["size"] != prev.get("size")) or (abs(attrs["mtime"] - prev.get("mtime", 0)) > 1):
                new_or_changed.append(filepath)
    # Update state with current files
    state[watcher_id] = current_files
    return new_or_changed

# ── Optional processing command ─────────────────────────────────
def process_file(command_template: str, filepath: str) -> None:
    """Run a command substituting {filepath}."""
    try:
        cmd = command_template.replace("{filepath}", filepath)
        subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        _post(f"Failed to run processing command: {e}", "warning")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("File Watcher Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        watchers = config.get("watchers", [])
        poll_interval = int(config.get("poll_interval_seconds", 60))
        state_file = config.get("state_file", "file_watcher_state.json")
        state = load_state(state_file)

        for watcher in watchers:
            wid = watcher.get("id", "unknown")
            wtype = watcher.get("type")
            extensions = [ext.lower() for ext in watcher.get("extensions", [])]
            try:
                current_files = {}
                if wtype == "local":
                    path = watcher.get("path")
                    if path and os.path.isdir(path):
                        current_files = get_local_files(path, extensions)
                elif wtype == "s3":
                    current_files = get_s3_files(
                        bucket=watcher["bucket"],
                        prefix=watcher.get("prefix", ""),
                        extensions=extensions,
                        aws_config=watcher
                    )
                elif wtype == "sftp":
                    current_files = get_sftp_files(
                        host=watcher["host"],
                        port=int(watcher.get("port", 22)),
                        username=watcher["username"],
                        password=watcher.get("password", ""),
                        remote_path=watcher["remote_path"],
                        extensions=extensions
                    )
                else:
                    _post(f"Unknown watcher type '{wtype}' for {wid}", "warning")
                    continue

                new_files = detect_new_files(wid, current_files, state)
                for filepath in new_files:
                    _post(f"New file detected: {filepath} (watcher {wid})", "info",
                          {"watcher": wid, "file": filepath})
                    cmd = watcher.get("process_command")
                    if cmd:
                        process_file(cmd, filepath)
                        _post(f"Triggered process command for {filepath}", "info")
            except Exception as e:
                _post(f"Error processing watcher {wid}: {e}", "warning")

        save_state(state_file, state)
        _heartbeat()
        time.sleep(poll_interval)

if __name__ == "__main__":
    main()
