#!/usr/bin/env python3
"""
code_synthesis_bot.py — Code Synthesis Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Listens for critiques from the Self‑Critic Bot, then
uses an LLM to generate a fixed version of a broken
scraper script, tests it in a sandbox, and deploys
it if the test passes.

Attachable to the Nazgul BotController.

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `code_synthesis_config.json` in the same directory:

{
  "db_path": "/path/to/botcontroller.db",
  "llm": {
    "provider": "openai",
    "api_key": "sk-...",
    "model": "gpt-4o-mini",
    "temperature": 0.0,
    "max_tokens": 4000,
    "endpoint": null
  },
  "poll_interval_minutes": 5,
  "critic_bot_id": "self_critic_bot",
  "trigger_keywords": ["Recommendation", "fix", "filter"],
  "scripts": {
    "momentum_chaser_bot": "/path/to/momentum_chaser_bot.py",
    "lead_bot": "/path/to/lead_bot.py"
  },
  "sandbox": {
    "timeout": 30,
    "test_duration": 5
  },
  "backup": true
}
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "code_synthesis_bot"
BOT_NAME = "Code Synthesis"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "code_synthesis_config.json"
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
def fetch_recent_critic_messages(db_path: str, since: datetime,
                                 critic_bot_id: str, keywords: list) -> list[dict]:
    """Return messages from the critic bot that contain any of the trigger keywords."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT id, bot_id, summary, payload, ts FROM messages "
            "WHERE bot_id = ? AND ts >= ? ORDER BY ts",
            (critic_bot_id, since.isoformat())
        )
        rows = cursor.fetchall()
        conn.close()
        filtered = []
        for row in rows:
            summary = row["summary"] or ""
            if any(kw.lower() in summary.lower() for kw in keywords):
                filtered.append(dict(row))
        return filtered
    except Exception as e:
        _post(f"DB read error: {e}", "warning")
        return []

def get_last_check_time(state_file: str) -> datetime:
    """Load the timestamp of the last scan from a state file."""
    try:
        with open(state_file, "r") as f:
            data = json.load(f)
            return datetime.fromisoformat(data["last_check"])
    except Exception:
        return datetime.now(timezone.utc) - timedelta(minutes=5)

def save_last_check_time(state_file: str, dt: datetime) -> None:
    with open(state_file, "w") as f:
        json.dump({"last_check": dt.isoformat()}, f)

# ── LLM code generation ──────────────────────────────────────────────────────
def call_llm(prompt: str, config: dict) -> str | None:
    llm_cfg = config.get("llm", {})
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
            "max_tokens": int(llm_cfg.get("max_tokens", 4000)),
        }
        resp = requests.post(endpoint, headers=headers, json=data, timeout=30)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
        else:
            _post(f"LLM error: {resp.status_code} {resp.text[:200]}", "warning")
            return None
    except Exception as e:
        _post(f"LLM call failed: {e}", "warning")
        return None

def extract_code(llm_response: str) -> str:
    """Extract code from a typical LLM response (remove markdown fences)."""
    code = llm_response.strip()
    if code.startswith("```python"):
        lines = code.splitlines()
        # Remove first and last ``` lines
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        code = "\n".join(lines)
    elif code.startswith("```"):
        # Catch all without language specifier
        lines = code.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        code = "\n".join(lines)
    return code.strip()

# ── Sandbox test ──────────────────────────────────────────────────────────────
def test_script(new_script_content: str, sandbox_config: dict) -> bool:
    """Run the generated script in a sandbox and return True if it starts without immediate crash."""
    timeout = int(sandbox_config.get("timeout", 30))
    test_duration = int(sandbox_config.get("test_duration", 5))

    try:
        # Create a temporary directory
        with tempfile.TemporaryDirectory() as tmpdir:
            script_path = os.path.join(tmpdir, "test_script.py")
            with open(script_path, "w") as f:
                f.write(new_script_content)
            # Make it executable if needed?
            # Run the script with a timeout
            proc = subprocess.Popen(
                ["python3", script_path],
                cwd=tmpdir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                # Wait a few seconds to see if it crashes immediately
                proc.wait(timeout=test_duration)
                # If it returns quickly, check returncode
                if proc.returncode != 0:
                    stdout, stderr = proc.communicate(timeout=1)
                    _post(f"Sandbox test: script exited with code {proc.returncode}. stderr: {(stderr or '')[:200]}", "warning")
                    return False
                else:
                    # It completed normally (which might be fine if it's a short-lived job)
                    return True
            except subprocess.TimeoutExpired:
                # Still running after test_duration – good sign
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return True
    except Exception as e:
        _post(f"Sandbox test error: {e}", "warning")
        return False

# ── Deployment ────────────────────────────────────────────────────────────────
def deploy_script(bot_id: str, new_script: str, script_config: dict,
                  backup: bool) -> bool:
    script_path = script_config.get(bot_id)
    if not script_path:
        _post(f"No script path configured for {bot_id}", "error")
        return False
    original = Path(script_path)
    if not original.exists():
        _post(f"Original script not found at {script_path}", "error")
        return False

    try:
        # Backup
        if backup:
            backup_path = original.with_suffix(".bak")
            shutil.copy2(original, backup_path)
            _post(f"Backed up {original} to {backup_path}", "info")

        # Write new script
        with open(original, "w") as f:
            f.write(new_script)
        _post(f"Deployed new script for {bot_id} at {original}", "info")
        return True
    except Exception as e:
        _post(f"Deployment error for {bot_id}: {e}", "error")
        return False

# ── Main logic ────────────────────────────────────────────────────────────────
def main():
    _post("Code Synthesis Bot online")
    state_file = "code_synthesis_state.json"
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

        poll_minutes = int(config.get("poll_interval_minutes", 5))
        critic_id = config.get("critic_bot_id", "self_critic_bot")
        keywords = config.get("trigger_keywords", ["Recommendation", "fix", "filter"])
        scripts_cfg = config.get("scripts", {})
        sandbox_cfg = config.get("sandbox", {})
        do_backup = config.get("backup", True)

        last_check = get_last_check_time(state_file)
        now = datetime.now(timezone.utc)

        messages = fetch_recent_critic_messages(db_path, last_check, critic_id, keywords)
        save_last_check_time(state_file, now)

        for msg in messages:
            critic_summary = msg["summary"]
            payload_str = msg.get("payload")
            # Try to extract the target bot_id from the payload
            target_bot = None
            if payload_str:
                try:
                    payload = json.loads(payload_str)
                    target_bot = payload.get("bot_id")
                except Exception:
                    pass
            # Fallback: try to find a bot_id in the summary (e.g., "lead_bot:")
            if not target_bot:
                # Simple heuristic: look for known bot_ids in summary
                for bid in scripts_cfg.keys():
                    if bid.lower() in critic_summary.lower():
                        target_bot = bid
                        break
            if not target_bot:
                _post(f"Could not determine target bot from critique: {critic_summary[:100]}", "warning")
                continue

            script_path = scripts_cfg.get(target_bot)
            if not script_path:
                _post(f"No script path configured for {target_bot}", "error")
                continue
            if not os.path.exists(script_path):
                _post(f"Script for {target_bot} not found at {script_path}", "error")
                continue

            # Read original script content
            try:
                with open(script_path, "r") as f:
                    original_code = f.read()
            except Exception as e:
                _post(f"Could not read original script: {e}", "error")
                continue

            # Build LLM prompt
            prompt = (
                f"The following Python scraping/monitoring script has been flagged by a performance analyst.\n\n"
                f"Original script ({target_bot}):\n```python\n{original_code}\n```\n\n"
                f"Analysis and recommendation:\n{critic_summary}\n\n"
                f"Write a complete, corrected version of the script that addresses the recommendation "
                f"while preserving all other functionality. Output ONLY the corrected Python code, "
                f"enclosed in a code block (```python ... ```). Do not include explanations."
            )

            new_code = call_llm(prompt, config)
            if not new_code:
                _post(f"LLM failed to generate code for {target_bot}", "error")
                continue

            clean_code = extract_code(new_code)
            if not clean_code:
                _post(f"LLM response did not contain usable code for {target_bot}", "error")
                continue

            _post(f"Generated new code for {target_bot}, testing in sandbox...", "info")
            if not test_script(clean_code, sandbox_cfg):
                _post(f"Sandbox test FAILED for {target_bot}. Code not deployed.", "warning")
                continue

            # Test passed – deploy
            if deploy_script(target_bot, clean_code, scripts_cfg, do_backup):
                _post(f"Successfully deployed fixed {target_bot}", "info")
            else:
                _post(f"Deployment FAILED for {target_bot}", "error")

        _heartbeat()
        time.sleep(poll_minutes * 60)

if __name__ == "__main__":
    main()
