#!/usr/bin/env python3
"""
self_critic_bot.py — Self-Critic Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Analyzes the performance of other bots using the
BotController's message history and an LLM to generate
constructive critiques and hypotheses.

If a valid OpenAI API key is provided, the bot will
ask the model for insights. If not, it will post
raw performance summaries instead.

Attachable to the Nazgul BotController.

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `self_critic_config.json` in the same directory:

{
  "db_path": "/path/to/botcontroller.db",
  "lookback_hours": 6,
  "bots_include": ["*"],            // or ["lead_bot", "momentum_chaser_bot"]
  "bots_exclude": ["self_critic_bot"],
  "llm": {
    "provider": "openai",           // only "openai" for now
    "api_key": "sk-...",            // reqd for OpenAI
    "model": "gpt-4o-mini",
    "temperature": 0.0,
    "max_tokens": 400,
    "endpoint": null                // custom endpoint (for proxies)
  },
  "prompt_template": "You are a performance analyst. ...",
  "poll_interval_minutes": 60
}
"""

import json
import os
import time
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "self_critic_bot"
BOT_NAME = "Self‑Critic"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "self_critic_config.json"
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

# ── Database access ───────────────────────────────────────────────────────────
def fetch_messages_since(db_path: str, since: datetime,
                         bot_include: list[str], bot_exclude: list[str]) -> list[dict]:
    """Return messages after `since` optionally filtered by bot_id."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        params = [since.isoformat()]
        where = "ts >= ?"
        # If include list is not ["*"], add OR conditions
        if bot_include and "*" not in bot_include:
            placeholders = ",".join("?" for _ in bot_include)
            where += f" AND bot_id IN ({placeholders})"
            params.extend(bot_include)
        if bot_exclude:
            placeholders = ",".join("?" for _ in bot_exclude)
            where += f" AND bot_id NOT IN ({placeholders})"
            params.extend(bot_exclude)
        cursor = conn.execute(
            f"SELECT id, bot_id, bot_name, ts, summary, level, payload FROM messages WHERE {where} ORDER BY ts",
            params
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        _post(f"DB read error: {e}", "warning")
        return []

# ── Performance aggregation ───────────────────────────────────────────────────
def aggregate_performance(messages: list[dict]) -> dict:
    """
    Return per-bot metrics: message count, error count,
    recent errors, last summaries, and numeric values from payloads.
    """
    bots = defaultdict(lambda: {"total":0, "errors":0, "last_errors":[], "summaries":[], "numeric_payload":[]})
    for m in messages:
        bot = m["bot_id"]
        bots[bot]["total"] += 1
        bots[bot]["summaries"].append(m["summary"])
        if len(bots[bot]["summaries"]) > 10:
            bots[bot]["summaries"].pop(0)
        if m["level"] == "error":
            bots[bot]["errors"] += 1
            bots[bot]["last_errors"].append(m["summary"])
            if len(bots[bot]["last_errors"]) > 5:
                bots[bot]["last_errors"].pop(0)
        # Try to extract numeric metrics from payload
        if m["payload"]:
            try:
                payload = json.loads(m["payload"])
                if isinstance(payload, dict):
                    for k, v in payload.items():
                        if isinstance(v, (int, float)):
                            bots[bot]["numeric_payload"].append((k, v, m["summary"]))
                            if len(bots[bot]["numeric_payload"]) > 20:
                                bots[bot]["numeric_payload"].pop(0)
            except Exception:
                pass
    return dict(bots)

# ── LLM call ─────────────────────────────────────────────────────────────────
def ask_llm(prompt: str, config: dict) -> str | None:
    llm_cfg = config.get("llm", {})
    provider = llm_cfg.get("provider", "openai")
    api_key = llm_cfg.get("api_key")
    if not api_key:
        return None
    try:
        endpoint = llm_cfg.get("endpoint") or "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        data = {
            "model": llm_cfg.get("model", "gpt-4o-mini"),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": float(llm_cfg.get("temperature", 0.0)),
            "max_tokens": int(llm_cfg.get("max_tokens", 400)),
        }
        resp = requests.post(endpoint, headers=headers, json=data, timeout=20)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
        else:
            _post(f"LLM API error {resp.status_code}: {resp.text[:200]}", "warning")
            return None
    except Exception as e:
        _post(f"LLM call failed: {e}", "warning")
        return None

# ── Prompt creation ──────────────────────────────────────────────────────────
def build_prompt(bot_id: str, stats: dict, template: str) -> str:
    """Fill the template with bot stats."""
    # Stats contains keys: total, errors, summaries, last_errors, numeric_payload
    summary_lines = "\n".join(f"  • {s}" for s in stats.get("summaries", [])[-5:])
    error_lines   = "\n".join(f"  • {e}" for e in stats.get("last_errors", [])[-5:])
    numeric_lines = "\n".join(f"  • {k}: {v} (context: {ctx})" for k,v,ctx in stats.get("numeric_payload", [])[-5:])
    prompt = template.replace("{bot_id}", bot_id)\
                     .replace("{total_messages}", str(stats.get("total", 0)))\
                     .replace("{error_count}", str(stats.get("errors", 0)))\
                     .replace("{recent_summaries}", summary_lines)\
                     .replace("{recent_errors}", error_lines)\
                     .replace("{numeric_metrics}", numeric_lines)
    return prompt

# ── Main loop ────────────────────────────────────────────────────────────────
def main():
    _post("Self‑Critic Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        db_path = config.get("db_path")
        if not db_path or not os.path.exists(db_path):
            _post(f"Database not found: {db_path}", "error")
            time.sleep(60)
            continue

        lookback_hours = int(config.get("lookback_hours", 6))
        include = config.get("bots_include", ["*"])
        exclude = config.get("bots_exclude", [])
        poll_minutes = int(config.get("poll_interval_minutes", 60))

        since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        messages = fetch_messages_since(db_path, since, include, exclude)
        if not messages:
            _post("No recent bot messages to analyze", "info")
            _heartbeat()
            time.sleep(poll_minutes * 60)
            continue

        perf = aggregate_performance(messages)
        prompt_template = config.get("prompt_template", 
            "You are a constructive analyst. Review the following performance data for bot {bot_id}:\n"
            "Total messages: {total_messages}\nErrors: {error_count}\nRecent summaries:\n{recent_summaries}\n"
            "Recent errors:\n{recent_errors}\nNumeric metrics:\n{numeric_metrics}\n"
            "Provide a very brief hypothesis and a concrete recommendation (max 3 sentences). Be constructive."
        )

        for bot_id, stats in perf.items():
            prompt = build_prompt(bot_id, stats, prompt_template)
            critique = ask_llm(prompt, config)
            if critique:
                _post(f"{bot_id}: {critique}", "info", {"bot_id": bot_id, "stats": stats})
            else:
                # Fallback: post raw stats
                _post(f"{bot_id}: {stats['total']} msgs, {stats['errors']} errors (no LLM)",
                      "info", {"bot_id": bot_id, "stats": stats})

        _heartbeat()
        time.sleep(poll_minutes * 60)

if __name__ == "__main__":
    main()
