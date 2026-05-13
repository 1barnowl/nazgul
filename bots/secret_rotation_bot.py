#!/usr/bin/env python3
"""
secret_rotation_bot.py — Secret Rotation Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Automatically rotates API keys, database passwords,
and OAuth tokens using HashiCorp Vault.

All rotation events are reported to the Nazgul BotController
Hub (http://localhost:8765).

Requirements
────────────
    pip install hvac requests

Configuration
─────────────
Place `rotation_config.json` in the same directory as this script:

{
  "vault": {
    "url": "https://vault.example.com:8200",
    "token": "s.YOUR_VAULT_TOKEN",
    "namespace": null
  },
  "rotations": [
    {
      "name": "github_api_key",
      "type": "kv",
      "kv_path": "secret/data/api_keys/github",
      "key": "token",
      "rotation_days": 30,
      "generate_command": null
    },
    {
      "name": "postgres_root",
      "type": "database_root",
      "engine_path": "database",
      "role": "my-postgres-connection"
    },
    {
      "name": "aws_root_rotate",
      "type": "aws_root",
      "engine_path": "aws"
    }
  ],
  "scan_interval": 86400
}
"""

import json
import os
import secrets
import subprocess
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import hvac
import requests

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "secret_rotation_bot"
BOT_NAME = "Secret Rotation Bot"

HEARTBEAT_INTERVAL = 30
SCAN_INTERVAL = 3600  # default fallback
_last_hb = 0.0

# ── Configuration path ────────────────────────────────────────────────────────
CONFIG_NAME = "rotation_config.json"
CONFIG_PATH = Path(__file__).with_name(CONFIG_NAME)
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(CONFIG_NAME)

# ── Metadata storage within Vault ─────────────────────────────────────────────
META_MOUNT = "secret"          # where rotation metadata lives
META_PREFIX = f"{META_MOUNT}/data/rotation_meta/"

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

# ── Vault client ──────────────────────────────────────────────────────────────
def get_vault_client(cfg):
    try:
        client = hvac.Client(url=cfg["url"], token=cfg.get("token"))
        if cfg.get("namespace"):
            client.namespace = cfg["namespace"]
        # Test authentication
        if not client.is_authenticated():
            return None
        return client
    except Exception:
        return None

# ── Rotation metadata helpers ─────────────────────────────────────────────────
def _meta_path(name: str) -> str:
    return f"{META_PREFIX}{name}"

def get_last_rotation(vault: hvac.Client, name: str) -> datetime | None:
    try:
        resp = vault.secrets.kv.v2.read_secret_version(
            path=f"rotation_meta/{name}",
            mount_point=META_MOUNT,
        )
        data = resp["data"]["data"]
        iso = data.get("last_rotation")
        return datetime.fromisoformat(iso)
    except Exception:
        return None

def set_last_rotation(vault: hvac.Client, name: str, dt: datetime) -> None:
    try:
        vault.secrets.kv.v2.create_or_update_secret(
            path=f"rotation_meta/{name}",
            secret={"last_rotation": dt.isoformat()},
            mount_point=META_MOUNT,
        )
    except Exception as e:
        _post(f"Failed to update rotation metadata for {name}: {e}", "warning")

# ── Random string generation ──────────────────────────────────────────────────
def _random_value(length=48):
    """Generate a cryptographically random hex string."""
    return secrets.token_hex(length // 2)

# ── KV rotation ───────────────────────────────────────────────────────────────
def rotate_kv(vault: hvac.Client, rule: dict) -> bool:
    name = rule["name"]
    kv_path = rule["kv_path"]
    key = rule["key"]
    generate_command = rule.get("generate_command")

    try:
        # Read current secret (any version)
        mount, *rest = kv_path.split("/data/", 1)
        if len(rest) != 1:
            _post(f"Invalid kv_path for {name}: {kv_path}", "error")
            return False
        path = rest[0]
        resp = vault.secrets.kv.v2.read_secret_version(
            path=path,
            mount_point=mount,
        )
        current_data = resp["data"]["data"]
    except Exception as e:
        _post(f"Could not read current secret for {name}: {e}", "error")
        return False

    # Generate new value
    if generate_command:
        try:
            new_value = subprocess.check_output(generate_command, shell=True,
                                                text=True).strip()
        except subprocess.CalledProcessError as e:
            _post(f"Generate command failed for {name}: {e}", "error")
            return False
    else:
        new_value = _random_value()

    # Update the secret with new value
    new_data = {**current_data, key: new_value}
    try:
        vault.secrets.kv.v2.create_or_update_secret(
            path=path,
            secret=new_data,
            mount_point=mount,
        )
    except Exception as e:
        _post(f"Failed to update secret for {name}: {e}", "error")
        return False

    # Record rotation time
    set_last_rotation(vault, name, datetime.now(timezone.utc))
    _post(f"Rotated {name}: {key} updated in {kv_path}", "info")
    return True

# ── Database root rotation ────────────────────────────────────────────────────
def rotate_database_root(vault: hvac.Client, rule: dict) -> bool:
    name = rule["name"]
    engine_path = rule["engine_path"]
    role = rule.get("role")
    try:
        # Use appropriate Vault API: /database/rotate-root/:name
        # hvac might have a method vault.database.rotate_root_credentials(connection_name)
        vault.database.rotate_root_credentials(connection_name=role,
                                                mount_point=engine_path)
    except Exception as e:
        _post(f"Database root rotation failed for {name}: {e}", "error")
        return False
    set_last_rotation(vault, name, datetime.now(timezone.utc))
    _post(f"Rotated database root for {name} (role {role})", "info")
    return True

# ── AWS root rotation ─────────────────────────────────────────────────────────
def rotate_aws_root(vault: hvac.Client, rule: dict) -> bool:
    name = rule["name"]
    engine_path = rule["engine_path"]
    try:
        # For AWS secrets engine, root rotation endpoint: /aws/config/rotate-root
        vault.aws.rotate_root(mount_point=engine_path)
    except Exception as e:
        _post(f"AWS root rotation failed for {name}: {e}", "error")
        return False
    set_last_rotation(vault, name, datetime.now(timezone.utc))
    _post(f"Rotated AWS root credentials for {name}", "info")
    return True

# ── Rotation dispatcher ───────────────────────────────────────────────────────
ROTATION_HANDLERS = {
    "kv": rotate_kv,
    "database_root": rotate_database_root,
    "aws_root": rotate_aws_root,
}

def check_and_rotate(vault: hvac.Client, rule: dict):
    name = rule["name"]
    rot_type = rule.get("type", "kv")
    rotation_days = int(rule.get("rotation_days", 30))
    now = datetime.now(timezone.utc)

    last = get_last_rotation(vault, name)
    if last and (now - last) < timedelta(days=rotation_days):
        # Not yet due
        return

    handler = ROTATION_HANDLERS.get(rot_type)
    if not handler:
        _post(f"Unsupported rotation type '{rot_type}' for {name}", "error")
        return

    success = handler(vault, rule)
    if not success:
        _post(f"Rotation failed for {name}", "error")

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _post("Secret Rotation Bot online")
    global SCAN_INTERVAL

    try:
        with open(CONFIG_PATH, "r") as f:
            config = json.load(f)
    except Exception as e:
        _post(f"Could not load {CONFIG_PATH}: {e}", "error")
        return

    SCAN_INTERVAL = int(config.get("scan_interval", 86400))
    vault_cfg = config.get("vault", {})
    rotations = config.get("rotations", [])

    while True:
        try:
            vault = get_vault_client(vault_cfg)
            if not vault:
                _post("Cannot authenticate to Vault", "error")
            else:
                for rule in rotations:
                    try:
                        check_and_rotate(vault, rule)
                    except Exception as e:
                        _post(f"Unexpected error processing {rule.get('name')}: {e}",
                              "error")
        except Exception as e:
            _post(f"Top-level error: {e}", "error")

        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()
