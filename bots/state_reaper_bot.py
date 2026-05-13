#!/usr/bin/env python3
"""
state_reaper_bot.py — State Reaper Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Cleans up expired/tombstone keys in Redis and archives
old event logs to S3 (or Glacier via S3 lifecycle).

Attachable to the Nazgul BotController. All actions
reported via Hub (http://localhost:8765).

Configuration
─────────────
Place `reaper_config.json` in the same directory:

{
  "redis": {
    "host": "localhost",
    "port": 6379,
    "db": 0,
    "password": null
  },
  "aws": {
    "region": "us-east-1",
    "access_key_id": "YOUR_KEY",
    "secret_access_key": "YOUR_SECRET",
    "endpoint_url": null   # optional, for MinIO etc.
  },
  "reap": [
    {
      "pattern": "cache:*",
      "field": "expires_at",       # field name in JSON value
      "older_than_days": 1,
      "enabled": true
    }
  ],
  "archives": [
    {
      "source": "event_log",       # Redis list key
      "destination_bucket": "my-logs-bucket",
      "destination_prefix": "event_logs/",
      "retention_days": 7,
      "enabled": true
    }
  ],
  "scan_interval": 3600
}

Requirements
────────────
    pip install redis boto3 requests
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import redis
import boto3
from botocore.exceptions import BotoCoreError, ClientError
import requests

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "state_reaper_bot"
BOT_NAME = "State Reaper"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

# ── Configuration ─────────────────────────────────────────────────────────────
CONFIG_NAME = "reaper_config.json"
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

# ── Redis client ──────────────────────────────────────────────────────────────
def get_redis(cfg):
    if not cfg:
        return None
    try:
        return redis.Redis(
            host=cfg.get("host", "localhost"),
            port=cfg.get("port", 6379),
            db=cfg.get("db", 0),
            password=cfg.get("password") or None,
            socket_connect_timeout=3,
            socket_timeout=5,
        )
    except Exception:
        return None

# ── S3 client ─────────────────────────────────────────────────────────────────
def get_s3(cfg):
    if not cfg:
        return None
    try:
        session = boto3.Session(
            aws_access_key_id=cfg.get("access_key_id"),
            aws_secret_access_key=cfg.get("secret_access_key"),
            region_name=cfg.get("region", "us-east-1"),
        )
        return session.client("s3", endpoint_url=cfg.get("endpoint_url"))
    except Exception:
        return None

# ── Reap expired keys ─────────────────────────────────────────────────────────
def _is_expired(value_bytes, field, older_than_days):
    """Check if the given field in the JSON value is older than X days from now."""
    try:
        data = json.loads(value_bytes)
        ts = data.get(field)
        if not ts:
            return False
        # Support both ISO format string and numeric epoch
        if isinstance(ts, (int, float)):
            expire_time = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            expire_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
        return expire_time < cutoff
    except Exception:
        return False

def reap_keys(r: redis.Redis, reap_rules: list) -> dict:
    """Iterate keys matching patterns, delete expired ones. Return stats."""
    stats = {"scanned": 0, "deleted": 0, "errors": 0}
    for rule in reap_rules:
        if not rule.get("enabled", True):
            continue
        pattern = rule.get("pattern", "*")
        field = rule.get("field")
        days = rule.get("older_than_days", 30)
        if not field:
            continue

        try:
            cursor = 0
            while True:
                cursor, keys = r.scan(cursor, match=pattern, count=100)
                stats["scanned"] += len(keys)
                for key in keys:
                    try:
                        val = r.get(key)
                        if val and _is_expired(val, field, days):
                            r.delete(key)
                            stats["deleted"] += 1
                    except Exception:
                        stats["errors"] += 1
                if cursor == 0:
                    break
        except Exception:
            stats["errors"] += 1
    return stats

# ── Archive old logs ──────────────────────────────────────────────────────────
def archive_logs(r: redis.Redis, s3, archive_rules: list) -> dict:
    """Fetch old items from Redis lists, upload to S3, trim list. Return stats."""
    stats = {"processed": 0, "archived_items": 0, "bytes_uploaded": 0, "errors": 0}
    for rule in archive_rules:
        if not rule.get("enabled", True):
            continue
        source_key = rule.get("source")
        bucket = rule.get("destination_bucket")
        prefix = rule.get("destination_prefix", "")
        retention_days = rule.get("retention_days", 7)
        if not source_key or not bucket:
            continue

        try:
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(days=retention_days)

            # Fetch all items (inefficient for huge lists, but practical for moderate ones)
            # Alternatively, use LRANGE with known length; we'll assume reasonable size.
            length = r.llen(source_key)
            if length == 0:
                continue

            items = r.lrange(source_key, 0, -1)
            old_items = []
            new_items = []
            for item in items:
                try:
                    obj = json.loads(item.decode("utf-8"))
                    ts_value = obj.get("timestamp") or obj.get("ts")
                    if not ts_value:
                        new_items.append(item)
                        continue
                    if isinstance(ts_value, (int, float)):
                        ts = datetime.fromtimestamp(ts_value, tz=timezone.utc)
                    else:
                        ts = datetime.fromisoformat(ts_value.replace("Z", "+00:00"))
                    if ts < cutoff:
                        old_items.append(item)
                    else:
                        new_items.append(item)
                except Exception:
                    # unparseable items treated as recent
                    new_items.append(item)

            if not old_items:
                stats["processed"] += 1
                continue

            # Upload to S3
            archive_data = b"\n".join(item if isinstance(item, bytes) else item.encode() for item in old_items) + b"\n"
            ts_str = now.strftime("%Y%m%d_%H%M%S")
            key = f"{prefix}{source_key}_{ts_str}.jsonl"
            try:
                s3.put_object(Bucket=bucket, Key=key, Body=archive_data)
                stats["bytes_uploaded"] += len(archive_data)
                stats["archived_items"] += len(old_items)
            except (BotoCoreError, ClientError) as e:
                _post(f"S3 upload failed for {source_key}: {e}", "error")
                stats["errors"] += 1
                continue

            # Trim list: delete old items from Redis list.
            # Doing this atomically: rebuild the list with new items only.
            pipe = r.pipeline()
            pipe.delete(source_key)
            if new_items:
                pipe.rpush(source_key, *new_items)
            try:
                pipe.execute()
                stats["processed"] += 1
            except Exception as e:
                # Rollback? The list is gone now, so data loss possible. Log error.
                _post(f"Redis trim failed for {source_key}: {e}", "error")
                stats["errors"] += 1
        except Exception as e:
            _post(f"Archive processing failed for rule {rule}: {e}", "error")
            stats["errors"] += 1
    return stats

# ── Main loop ─────────────────────────────────────────────────────────────────
def main() -> None:
    _post("State Reaper Bot online")
    config = None
    try:
        with open(CONFIG_PATH, "r") as f:
            config = json.load(f)
    except Exception as e:
        _post(f"Could not load {CONFIG_PATH}: {e}", "error")
        return

    redis_client = get_redis(config.get("redis", {}))
    s3_client = get_s3(config.get("aws", {}))
    scan_interval = int(config.get("scan_interval", 3600))

    while True:
        now = time.time()
        if redis_client:
            reap_rules = config.get("reap", [])
            if reap_rules:
                stats = reap_keys(redis_client, reap_rules)
                _post(f"Reaped keys: scanned {stats['scanned']}, deleted {stats['deleted']}, errors {stats['errors']}",
                      "info", stats)

            archive_rules = config.get("archives", [])
            if archive_rules and s3_client:
                astats = archive_logs(redis_client, s3_client, archive_rules)
                _post(f"Archived logs: processed {astats['processed']} rules, "
                      f"items {astats['archived_items']}, bytes {astats['bytes_uploaded']}, errors {astats['errors']}",
                      "info", astats)
            elif archive_rules and not s3_client:
                _post("Archive rules present but S3 client not available — check AWS config.", "warning")
        else:
            _post("Redis connection failed — reaper cannot run", "error")

        _heartbeat()
        time.sleep(scan_interval)

if __name__ == "__main__":
    main()
