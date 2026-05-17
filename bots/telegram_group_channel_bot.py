#!/usr/bin/env python3
"""
telegram_group_channel_bot.py — Telegram Group & Channel Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Auto‑posts in channels and groups, replies to discussions,
and manages a bot that can directly message users about
product updates.  Uses python‑telegram‑bot to interact with
the Telegram Bot API.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install python-telegram-bot requests

Configuration
─────────────
Place `telegram_bot_config.json` in the same directory:

{
  "telegram": {
    "token": "YOUR_BOT_TOKEN"
  },
  "publishing": {
    "enabled": true,
    "scheduled_messages_file": "telegram_scheduled_messages.json"
  },
  "monitoring": {
    "enabled": true,
    "keywords": ["buy", "recommend", "suggest", "tool", "help"],
    "reply_template": "Hi! I noticed you mentioned {keyword}. We have a tool that might interest you: https://example.com",
    "cooldown_seconds": 300,
    "max_replies_per_chat_per_hour": 2
  },
  "direct_messages": {
    "enabled": true,
    "admin_user_id": 123456789   // your Telegram user ID (to receive admin commands)
  },
  "state_file": "telegram_bot_state.json",
  "heartbeat_interval": 30
}

Scheduled messages file (`telegram_scheduled_messages.json`) – an array of objects:
[
  {
    "target_type": "group",           // "group", "channel", or "user"
    "target_id": -1001234567890,       // chat_id (supergroup/channel have negative IDs)
    "text": "Don't miss our weekend sale!",
    "scheduled_at": "2025-03-01T12:00:00Z"
  }
]
"""

import json
import os
import time
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Any

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "telegram_group_channel_bot"
BOT_NAME = "Telegram Group & Channel Bot"

HEARTBEAT_INTERVAL = 30

CONFIG_NAME = "telegram_bot_config.json"
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
    while True:
        try:
            requests.post(f"{HUB}/heartbeat/{BOT_ID}", json={
                "bot_name": BOT_NAME,
                "status": "online",
            }, timeout=3)
        except Exception:
            pass
        time.sleep(HEARTBEAT_INTERVAL)

# ── State management ────────────────────────────────────────────
class StateManager:
    def __init__(self, state_file: str):
        self.state_file = state_file
        self.data: dict = {}
        self._load()

    def _load(self):
        try:
            with open(self.state_file, "r") as f:
                self.data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {
                "cooldown_times": {},       # str(chat_id) -> timestamp
                "reply_counts": {},          # str(chat_id) -> int
                "hour_start": time.time(),
                "posted_scheduled_ids": []   # hashes of posted messages
            }

    def save(self):
        with open(self.state_file, "w") as f:
            json.dump(self.data, f, indent=2)

    def get_channel_cooldown(self, chat_id: int) -> float:
        return self.data["cooldown_times"].get(str(chat_id), 0)

    def set_channel_cooldown(self, chat_id: int, ts: float):
        self.data["cooldown_times"][str(chat_id)] = ts

    def get_reply_count(self, chat_id: int) -> int:
        if time.time() - self.data["hour_start"] > 3600:
            self.data["reply_counts"] = {}
            self.data["hour_start"] = time.time()
        return self.data["reply_counts"].get(str(chat_id), 0)

    def increment_reply_count(self, chat_id: int):
        cnt = self.get_reply_count(chat_id)
        self.data["reply_counts"][str(chat_id)] = cnt + 1

    def is_scheduled_posted(self, item_hash: str) -> bool:
        return item_hash in self.data["posted_scheduled_ids"]

    def mark_scheduled_posted(self, item_hash: str):
        self.data["posted_scheduled_ids"].append(item_hash)
        # Keep last 500
        self.data["posted_scheduled_ids"] = self.data["posted_scheduled_ids"][-500:]

# ── Telegram bot logic ───────────────────────────────────────────
class TelegramBot:
    def __init__(self, config: dict, state: StateManager):
        self.config = config
        self.state = state
        self.token = config["telegram"]["token"]
        self.application = Application.builder().token(self.token).build()

        # Register handlers
        self.application.add_handler(CommandHandler("send", self.cmd_send_message))  # admin command to DM
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

    async def cmd_send_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin command: /send user_id text..."""
        if not self.config.get("direct_messages", {}).get("enabled"):
            return
        admin_id = self.config["direct_messages"].get("admin_user_id")
        if not admin_id or update.effective_user.id != admin_id:
            await update.message.reply_text("You are not authorized.")
            return
        # Parse args: /send user_id text
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("Usage: /send <user_id> <text>")
            return
        user_id = int(args[0])
        text = " ".join(args[1:])
        try:
            await context.bot.send_message(chat_id=user_id, text=text)
            await update.message.reply_text(f"Message sent to user {user_id}.")
            _post(f"Admin sent DM to user {user_id}", "info")
        except Exception as e:
            await update.message.reply_text(f"Failed: {e}")
            _post(f"Failed to send DM to {user_id}: {e}", "error")

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Monitor messages in groups/channels for keywords and auto‑reply."""
        if not self.config.get("monitoring", {}).get("enabled"):
            return
        message = update.effective_message
        if not message or not message.text:
            return
        chat = update.effective_chat
        if chat.type not in ("group", "supergroup"):
            return  # only group chats (channels can't be replied to by bots without admin permissions? could, but we'll limit to groups)

        # Rate limiting
        chat_id = chat.id
        cooldown = float(self.config["monitoring"].get("cooldown_seconds", 300))
        now = time.time()
        last = self.state.get_channel_cooldown(chat_id)
        if now - last < cooldown:
            return
        max_per_hour = int(self.config["monitoring"].get("max_replies_per_chat_per_hour", 2))
        if self.state.get_reply_count(chat_id) >= max_per_hour:
            return

        # Check keywords
        keywords = self.config["monitoring"].get("keywords", [])
        if not keywords:
            return
        text_lower = message.text.lower()
        matched = next((kw for kw in keywords if kw.lower() in text_lower), None)
        if not matched:
            return

        reply_template = self.config["monitoring"].get("reply_template", "")
        if not reply_template:
            return
        reply = reply_template.replace("{keyword}", matched).replace("{link}", self.config.get("link", ""))

        try:
            await message.reply_text(reply)
            _post(f"Replied to message in chat {chat_id} (keyword: {matched})", "info",
                  {"chat_id": chat_id, "keyword": matched})
            self.state.set_channel_cooldown(chat_id, now)
            self.state.increment_reply_count(chat_id)
        except Exception as e:
            _post(f"Failed to reply in chat {chat_id}: {e}", "error")

# ── Scheduled message processing (background task) ───────────────
async def process_scheduled_messages(config: dict, state: StateManager, application: Application):
    """Run every 30 seconds, check for due scheduled messages and send them."""
    if not config.get("publishing", {}).get("enabled"):
        return
    file_path = config["publishing"].get("scheduled_messages_file", "telegram_scheduled_messages.json")
    if not os.path.exists(file_path):
        return
    try:
        with open(file_path, "r") as f:
            scheduled = json.load(f)
    except Exception as e:
        _post(f"Error reading scheduled messages: {e}", "error")
        return

    if not scheduled:
        return

    now = datetime.now(timezone.utc)
    remaining = []
    updated = False
    for item in scheduled:
        scheduled_at_str = item.get("scheduled_at")
        if not scheduled_at_str:
            remaining.append(item)
            continue
        try:
            scheduled_dt = datetime.fromisoformat(scheduled_at_str)
        except ValueError:
            remaining.append(item)
            continue

        item_hash = str(hash(json.dumps(item, sort_keys=True)))
        if state.is_scheduled_posted(item_hash):
            continue  # already sent

        if now - timedelta(seconds=30) <= scheduled_dt <= now + timedelta(seconds=30):
            target_type = item.get("target_type", "group")
            target_id = item.get("target_id")
            text = item.get("text", "")
            if not target_id or not text:
                _post("Invalid scheduled message entry", "warning")
                remaining.append(item)
                continue

            try:
                await application.bot.send_message(chat_id=target_id, text=text)
                _post(f"Scheduled message sent to {target_type} {target_id}", "info",
                      {"target_id": target_id, "text": text[:60]})
                state.mark_scheduled_posted(item_hash)
                updated = True
                # Do not add to remaining (it's sent)
                continue
            except Exception as e:
                _post(f"Failed to send scheduled message to {target_id}: {e}", "error")
                # keep for retry
        remaining.append(item)

    if updated:
        # Write back the remaining items (those not processed)
        with open(file_path, "w") as f:
            json.dump(remaining, f, indent=2)

async def scheduled_loop(config, state, application):
    while True:
        await process_scheduled_messages(config, state, application)
        state.save()
        await asyncio.sleep(30)

# ── Main entry point ─────────────────────────────────────────────
async def main_async():
    # Load configuration
    try:
        with open(CONFIG_PATH, "r") as f:
            config = json.load(f)
    except Exception as e:
        _post(f"Config error: {e}", "error")
        return

    token = config.get("telegram", {}).get("token")
    if not token:
        _post("Telegram bot token missing", "error")
        return

    state_file = config.get("state_file", "telegram_bot_state.json")
    state = StateManager(state_file)

    # Start heartbeat in a separate thread
    import threading
    threading.Thread(target=_heartbeat, daemon=True).start()

    bot = TelegramBot(config, state)

    # Run scheduled messages loop concurrently with polling
    async with bot.application:
        await bot.application.start()
        asyncio.create_task(scheduled_loop(config, state, bot.application))
        _post("Telegram bot started – polling for messages", "info")
        # Run polling (blocking)
        await bot.application.updater.start_polling()
        # Wait until shutdown
        while True:
            await asyncio.sleep(3600)

def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        _post("Bot stopped by user", "info")
    except Exception as e:
        _post(f"Bot crashed: {e}", "error")

if __name__ == "__main__":
    main()
