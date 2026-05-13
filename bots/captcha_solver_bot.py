#!/usr/bin/env python3
"""
captcha_solver_bot.py — CAPTCHA Solver Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Integrates with 2Captcha / Capsolver APIs to solve
CAPTCHAs on demand. Exposes an HTTP endpoint so other
bots can submit tasks and retrieve solutions.

Attachable to the Nazgul BotController.

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `captcha_config.json` in the same directory:

{
  "provider": "2captcha",            // "2captcha" or "capsolver"
  "api_key": "YOUR_API_KEY",
  "http_port": 9191,
  "poll_interval": 5,
  "max_pending_tasks": 100,
  "balance_check_interval": 600
}
"""

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import requests

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "captcha_solver_bot"
BOT_NAME = "CAPTCHA Solver"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "captcha_config.json"
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

# ── CAPTCHA providers ────────────────────────────────────────────────────────
class BaseProvider:
    def create_task(self, params: dict) -> str:
        raise NotImplementedError
    def get_result(self, task_id: str) -> dict | None:
        raise NotImplementedError
    def get_balance(self) -> float | None:
        raise NotImplementedError

class TwoCaptcha(BaseProvider):
    BASE_IN  = "https://api.2captcha.com/in.php"
    BASE_RES = "https://api.2captcha.com/res.php"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def create_task(self, params: dict) -> str:
        # Map from common params to 2Captcha's backend
        method = params.get("method", "userrecaptcha")
        key = self.api_key
        data = {"key": key, "method": method, "json": 1}
        if method == "userrecaptcha":
            data["googlekey"] = params.get("sitekey", "")
            data["pageurl"]   = params.get("pageurl", "")
        elif method == "hcaptcha":
            data["sitekey"] = params.get("sitekey", "")
            data["pageurl"] = params.get("pageurl", "")
        elif method == "imagecaptcha":
            data["body"] = params.get("body", "")  # base64
        else:
            # Generic: pass all params as-is (advanced)
            for k, v in params.items():
                if k not in ("method",):
                    data[k] = v
        try:
            resp = requests.post(self.BASE_IN, data=data, timeout=15)
            result = resp.json()
            if result.get("status") == 1:
                return str(result["request"])
            else:
                raise Exception(result.get("error_text", "unknown create error"))
        except Exception as e:
            raise Exception(f"2captcha create_task error: {e}") from e

    def get_result(self, task_id: str) -> dict | None:
        try:
            resp = requests.get(self.BASE_RES, params={
                "key": self.api_key,
                "action": "get",
                "id": task_id,
                "json": 1
            }, timeout=10)
            result = resp.json()
            if result.get("status") == 1:
                return {"solution": result["request"]}
            elif result.get("request") == "CAPCHA_NOT_READY":
                return None
            else:
                raise Exception(result.get("error_text", "unknown get error"))
        except Exception as e:
            raise Exception(f"2captcha get_result error: {e}") from e

    def get_balance(self) -> float | None:
        try:
            resp = requests.get(self.BASE_RES, params={
                "key": self.api_key,
                "action": "getbalance",
                "json": 1
            }, timeout=10)
            result = resp.json()
            if result.get("status") == 1:
                return float(result["request"])
        except Exception:
            pass
        return None

class Capsolver(BaseProvider):
    CREATE_URL = "https://api.capsolver.com/createTask"
    RESULT_URL = "https://api.capsolver.com/getTaskResult"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def create_task(self, params: dict) -> str:
        # Capsolver expects a JSON body with "clientKey", "task" (type, ...)
        task_type = params.get("type", "ReCaptchaV2TaskProxyLess")
        body = {
            "clientKey": self.api_key,
            "task": {
                "type": task_type,
            }
        }
        if task_type.startswith("ReCaptchaV"):
            body["task"]["websiteKey"] = params.get("sitekey", "")
            body["task"]["websiteURL"] = params.get("pageurl", "")
            if "proxy" in params:
                body["task"]["proxy"] = params["proxy"]
        elif task_type.startswith("HCaptcha"):
            body["task"]["websiteKey"] = params.get("sitekey", "")
            body["task"]["websiteURL"] = params.get("pageurl", "")
        elif task_type == "ImageToTextTask":
            body["task"]["body"] = params.get("body", "")
        else:
            # Copy all provided params under task
            for k, v in params.items():
                if k not in ("type",):
                    body["task"][k] = v
        try:
            resp = requests.post(self.CREATE_URL, json=body, timeout=15)
            result = resp.json()
            if result.get("errorId") == 0:
                return str(result["taskId"])
            else:
                raise Exception(result.get("errorDescription", "unknown create error"))
        except Exception as e:
            raise Exception(f"Capsolver create_task error: {e}") from e

    def get_result(self, task_id: str) -> dict | None:
        try:
            resp = requests.post(self.RESULT_URL, json={
                "clientKey": self.api_key,
                "taskId": int(task_id)
            }, timeout=10)
            result = resp.json()
            if result.get("errorId") == 0:
                status = result.get("status")
                if status == "ready":
                    return result["solution"]
                elif status == "processing":
                    return None
                else:
                    raise Exception(f"unexpected status {status}")
            else:
                raise Exception(result.get("errorDescription", "unknown get error"))
        except Exception as e:
            raise Exception(f"Capsolver get_result error: {e}") from e

    def get_balance(self) -> float | None:
        # Capsolver doesn't have a simple balance endpoint, but we can check via getBalance? Actually they have a dashboard.
        return None

# ── Task manager ──────────────────────────────────────────────────────────────
class TaskManager:
    def __init__(self, provider: BaseProvider, max_pending: int = 100):
        self.provider = provider
        self.max_pending = max_pending
        self.tasks: dict[str, dict] = {}   # local_id -> { provider_id, status, solution, params, created }
        self.lock = threading.Lock()

    def submit(self, captcha_params: dict) -> str:
        with self.lock:
            if len(self.tasks) >= self.max_pending:
                raise Exception("Task queue full")
            task_id = str(uuid.uuid4())
            try:
                provider_id = self.provider.create_task(captcha_params)
            except Exception as e:
                raise Exception(f"Failed to create captcha task: {e}") from e
            self.tasks[task_id] = {
                "provider_id": provider_id,
                "status": "pending",
                "solution": None,
                "params": captcha_params,
                "created": time.time()
            }
            return task_id

    def get_status(self, task_id: str) -> dict | None:
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return None
            return {
                "task_id": task_id,
                "status": task["status"],
                "solution": task["solution"],
                "created": task["created"]
            }

    def poll_pending(self):
        """Check all pending tasks for completion."""
        with self.lock:
            pending_ids = [tid for tid, t in self.tasks.items() if t["status"] == "pending"]
        for tid in pending_ids:
            try:
                provider_id = self.tasks[tid]["provider_id"]
                result = self.provider.get_result(provider_id)
                with self.lock:
                    if result is not None:
                        self.tasks[tid]["status"] = "solved"
                        self.tasks[tid]["solution"] = result
                    # else still pending
            except Exception as e:
                with self.lock:
                    self.tasks[tid]["status"] = "failed"
                    self.tasks[tid]["solution"] = str(e)

# ── HTTP API ─────────────────────────────────────────────────────────────────
class CaptchaHandler(BaseHTTPRequestHandler):
    manager: TaskManager = None

    def _json_response(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_POST(self):
        if self.path == "/solve":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                params = json.loads(body)
                task_id = self.manager.submit(params)
                self._json_response(202, {"task_id": task_id, "status": "pending"})
            except Exception as e:
                self._json_response(400, {"error": str(e)})
        else:
            self._json_response(404, {})

    def do_GET(self):
        if self.path.startswith("/result/"):
            task_id = self.path.split("/result/")[1].strip("/")
            status = self.manager.get_status(task_id)
            if status is None:
                self._json_response(404, {"error": "task not found"})
            else:
                self._json_response(200, status)
        else:
            self._json_response(404, {})

    def log_message(self, *args):
        pass

def start_http_api(manager: TaskManager, port: int):
    CaptchaHandler.manager = manager
    server = HTTPServer(("0.0.0.0", port), CaptchaHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"CAPTCHA Solver HTTP API started on port {port}", "info")

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _post("CAPTCHA Solver Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        provider_name = config.get("provider", "2captcha")
        api_key = config.get("api_key")
        if not api_key:
            _post("No API key configured", "error")
            time.sleep(60)
            continue

        if provider_name == "2captcha":
            provider = TwoCaptcha(api_key)
        elif provider_name == "capsolver":
            provider = Capsolver(api_key)
        else:
            _post(f"Unknown provider: {provider_name}", "error")
            time.sleep(60)
            continue

        max_pending = int(config.get("max_pending_tasks", 100))
        manager = TaskManager(provider, max_pending)
        http_port = int(config.get("http_port", 9191))
        start_http_api(manager, http_port)

        poll_interval = int(config.get("poll_interval", 5))
        balance_interval = int(config.get("balance_check_interval", 600))
        last_balance_check = 0

        while True:
            manager.poll_pending()
            # Periodically check balance and send to Hub
            now = time.time()
            if now - last_balance_check >= balance_interval:
                balance = provider.get_balance()
                if balance is not None:
                    _post(f"CAPTCHA API balance: ${balance:.2f}", "info")
                last_balance_check = now

            # Status summary
            with manager.lock:
                total = len(manager.tasks)
                pending = sum(1 for t in manager.tasks.values() if t["status"] == "pending")
                solved = sum(1 for t in manager.tasks.values() if t["status"] == "solved")
                failed = sum(1 for t in manager.tasks.values() if t["status"] == "failed")
            _post(f"Tasks: {total} total, {pending} pending, {solved} solved, {failed} failed", "info")

            _heartbeat()
            time.sleep(poll_interval)

if __name__ == "__main__":
    main()
