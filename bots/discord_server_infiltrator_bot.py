#!/usr/bin/env python3
"""
discord_server_infiltrator_bot.py — Discord Server Infiltrator Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors public Discord servers (once invited) for relevant
questions and auto‑replies with helpful messages, including links
where permitted.  Requires the bot to be added to servers via an
OAuth2 invite with the necessary permissions (Read Messages, Send
Messages, Read Message History).  Configure server IDs in the
config to restrict which servers are monitored.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install discord.py requests

Configuration
─────────────
Place `discord_infiltrator_config.json` in the same directory:

{
  "discord": {
    "bot_token": "YOUR_DISCORD_BOT_TOKEN"
  },
  "monitoring": {
    "server_ids": [123456789012345678, 987654321098765432],   // optional; if empty, all servers the bot is in
    "keywords": ["recommend", "suggest", "tool", "library", "framework", "need help", "how to", "alternative to"],
    "reply_template": "Hi! If you're looking for {keyword}, I'd recommend checking out our open‑source toolkit: https://example.com – it might be exactly what you need.",
    "cooldown_seconds": 300,                                 // per channel to avoid spam
    "max_replies_per_channel_per_hour": 2,
    "ignore_bots": true,
    "ignore_users": ["spammer#1234"]
  },
  "state_file": "discord_infiltrator_state.json",
  "heartbeat_interval": 30
}
"""

import json
import os
import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

import discord
import requests

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "discord_server_infiltrator_bot"
BOT_NAME = "Discord Server Infiltrator"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "discord_infiltrator_config.json"
CONFIG_PATH = Path(__file__).with_name(CONFIG_NAME)
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(CONFIG_NAME)

# ── Hub helpers ──────────────────────────────────────────────────
def _post(summary: str, level: str = "info", payload: dict = None) -> None:
    try:
        requests.post(f"{HUB}/ingest", json={
            "bot_id": BOT_ID,
            "bot_name": BOT_NAME,
            "summary": summary,
            "level": level,
            "payload": payload or {},
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
            "status": "online",
        }, timeout=3)
    except Exception:
        pass
    _last_hb = time.time()

# ── State (in‑memory + file backup) ────────────────────────────
class StateManager:
    def __init__(self, state_file: str):
        self.state_file = state_file
        self.channel_cooldowns: Dict[int, float] = {}
        self.channel_reply_counts: Dict[int, int] = {}
        self.last_hour_reset: float = time.time()
        self._load()

    def _load(self):
        try:
            with open(self.state_file, "r") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        self.channel_cooldowns = {int(k): v for k, v in data.get("channel_cooldowns", {}).items()}
        self.channel_reply_counts = {int(k): v for k, v in data.get("channel_reply_counts", {}).items()}

    def save(self):
        data = {
            "channel_cooldowns": self.channel_cooldowns,
            "channel_reply_counts": self.channel_reply_counts
        }
        with open(self.state_file, "w") as f:
            json.dump(data, f, indent=2)

    def is_channel_cooled_down(self, channel_id: int, cooldown_seconds: float) -> bool:
        now = time.time()
        last = self.channel_cooldowns.get(channel_id, 0)
        if now - last < cooldown_seconds:
            return True
        return False

    def record_reply(self, channel_id: int):
        now = time.time()
        self.channel_cooldowns[channel_id] = now
        # Reset hourly counts if needed
        if now - self.last_hour_reset > 3600:
            self.channel_reply_counts.clear()
            self.last_hour_reset = now
        self.channel_reply_counts[channel_id] = self.channel_reply_counts.get(channel_id, 0) + 1

    def too_many_replies(self, channel_id: int, max_per_hour: int) -> bool:
        if now := time.time() - self.last_hour_reset > 3600:
            self.channel_reply_counts.clear()
            self.last_hour_reset = now
        return self.channel_reply_counts.get(channel_id, 0) >= max_per_hour

# ── Discord client ───────────────────────────────────────────────
class InfiltratorBot(discord.Client):
    def __init__(self, config: dict, state_manager: StateManager, **kwargs):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, **kwargs)
        self.config = config
        self.state_manager = state_manager
        self.keywords: List[str] = config.get("monitoring", {}).get("keywords", [])
        self.reply_template: str = config.get("monitoring", {}).get("reply_template", "")
        self.cooldown_seconds: float = float(config.get("monitoring", {}).get("cooldown_seconds", 300))
        self.max_replies_per_channel_per_hour: int = int(config.get("monitoring", {}).get("max_replies_per_channel_per_hour", 2))
        self.ignore_bots: bool = config.get("monitoring", {}).get("ignore_bots", True)
        self.ignore_users: Set[str] = set(u.lower() for u in config.get("monitoring", {}).get("ignore_users", []))
        self.server_ids: Set[int] = set(config.get("monitoring", {}).get("server_ids", []))

    async def on_ready(self):
        _post(f"Logged in as {self.user} – monitoring {len(self.guilds)} servers", "info")
        # Start background heartbeat
        self.bg_task = asyncio.create_task(self.heartbeat_loop())

    async def heartbeat_loop(self):
        while True:
            _heartbeat()
            # Save state periodically
            self.state_manager.save()
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def on_message(self, message: discord.Message):
        if message.author == self.user:
            return
        # Check if bot should ignore other bots
        if self.ignore_bots and message.author.bot:
            return
        # Check ignored users
        if str(message.author).lower() in self.ignore_users:
            return
        # If server_ids is specified, only monitor those guilds
        if self.server_ids and (message.guild is None or message.guild.id not in self.server_ids):
            return

        # Rate limiting
        channel_id = message.channel.id
        if self.state_manager.is_channel_cooled_down(channel_id, self.cooldown_seconds):
            return
        if self.state_manager.too_many_replies(channel_id, self.max_replies_per_channel_per_hour):
            return

        # Check keywords in message content
        content_lower = message.content.lower()
        matched_keyword = next((kw for kw in self.keywords if kw.lower() in content_lower), None)
        if not matched_keyword:
            return

        # Build reply
        reply_text = self.reply_template.replace("{keyword}", matched_keyword).replace("{link}", self.config.get("link", ""))
        try:
            await message.channel.send(reply_text)
            _post(f"Replied to message from {message.author} in #{message.channel} (guild: {message.guild.name})", "info", {
                "guild": str(message.guild),
                "channel": str(message.channel),
                "author": str(message.author),
                "keyword": matched_keyword
            })
            self.state_manager.record_reply(channel_id)
        except discord.Forbidden:
            _post(f"Missing permissions to send message in #{message.channel} on {message.guild}", "warning")
        except Exception as e:
            _post(f"Failed to send reply: {e}", "error")

# ── Main entry point ─────────────────────────────────────────────
def main():
    _post("Discord Server Infiltrator Bot online")
    # Load config
    try:
        with open(CONFIG_PATH, "r") as f:
            config = json.load(f)
    except Exception as e:
        _post(f"Config error: {e}", "error")
        return

    token = config.get("discord", {}).get("bot_token")
    if not token:
        _post("Discord bot token missing", "error")
        return

    state_file = config.get("state_file", "discord_infiltrator_state.json")
    state_manager = StateManager(state_file)

    # Create and run client
    client = InfiltratorBot(config, state_manager)

    # Run the bot (blocking)
    try:
        client.run(token)
    except Exception as e:
        _post(f"Discord client crashed: {e}", "error")
        # Could restart here but for simplicity we'll exit
        time.sleep(60)
        # In a production bot you'd want a restart loop; we'll just log and stop
        _post("Bot shutting down due to error", "error")

if __name__ == "__main__":
    main()
