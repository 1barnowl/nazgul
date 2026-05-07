#!/usr/bin/env python3
"""
Nazgul — Standalone Desktop Bot Dashboard
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
No Redis. No FastAPI. No browser. Pure Python stdlib + Tkinter.
Works on Linux and Windows. One file, one executable.

BUILD
─────
    pip install pyinstaller
    Linux / macOS:
        pyinstaller --onefile --windowed --name BotController botcontroller.py
    Windows:
        pyinstaller --onefile --windowed --name BotController.exe botcontroller.py
    Executable lands in ./dist/

BOT PROTOCOL  (bot scripts POST to http://localhost:8765)
─────────────────────────────────────────────────────────
    POST /ingest
        {
          "bot_id":   "my_bot",          # unique identifier
          "bot_name": "My Bot",          # display name
          "summary":  "Something happened",
          "level":    "info|warning|error",
          "payload":  { ... }            # optional, any JSON
        }

    POST /heartbeat/{bot_id}
        {
          "bot_name": "My Bot",
          "status":   "online|degraded"
        }
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk


# ─── Constants ────────────────────────────────────────────────────────────────

HUB_PORT   = 8765          # embedded HTTP hub port
BOT_TTL    = 35            # seconds of silence → bot goes offline
POLL_MS    = 2500          # dashboard redraw interval (ms)
MAX_MEM    = 200           # max messages kept in RAM per bot
COL_MIN_W  = 295           # minimum pixel width of each bot column

STATUS_ICON = {"online": "🟢", "degraded": "🟡", "offline": "🔴"}
LEVEL_ICON  = {"info": "ℹ", "warning": "⚠", "error": "✖"}


# ─── Path helpers ─────────────────────────────────────────────────────────────

def _exe_dir() -> str:
    """Folder of the running executable (or script during development)."""
    if getattr(sys, "frozen", False):        # PyInstaller bundle
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


DB_PATH = os.path.join(_exe_dir(), "botcontroller.db")


def _system_python() -> str:
    """
    Return the system Python interpreter path.

    IMPORTANT: when running as a PyInstaller .exe, sys.executable points to the
    bundle itself — NOT to python3. We must locate the real interpreter on PATH.
    """
    if getattr(sys, "frozen", False):
        for name in ("python3", "python", "python3.12", "python3.11",
                     "python3.10", "python3.9"):
            exe = shutil.which(name)
            if exe:
                return exe
        return "python3"          # last-ditch fallback
    return sys.executable         # dev mode: use this very interpreter


# ─── Global in-memory state ───────────────────────────────────────────────────

_lock = threading.Lock()

# bot_id → { name, status, last_seen(float), messages:[{ts,summary,level}, ...] }
_bots: dict[str, dict] = {}


def _new_bot_record(bot_id: str, name: str) -> dict:
    return {
        "name":      name,
        "status":    "online",
        "last_seen": time.time(),
        "messages":  [],
    }


# ─── SQLite persistence ───────────────────────────────────────────────────────

@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def db_init() -> None:
    with _db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id    TEXT NOT NULL,
                bot_name  TEXT,
                ts        TEXT NOT NULL,
                summary   TEXT NOT NULL,
                payload   TEXT,
                level     TEXT DEFAULT 'info'
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_bid ON messages(bot_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ts  ON messages(ts)")


def db_insert(bot_id: str, bot_name: str, summary: str,
              level: str, payload) -> str:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _db() as c:
        c.execute(
            "INSERT INTO messages(bot_id,bot_name,ts,summary,level,payload)"
            " VALUES(?,?,?,?,?,?)",
            (bot_id, bot_name, ts, summary, level,
             json.dumps(payload) if payload is not None else None),
        )
    return ts


def db_get_messages(bot_id: str, limit: int = 60,
                    keyword: str | None = None,
                    level: str | None = None) -> list[dict]:
    where, params = ["bot_id=?"], [bot_id]
    if keyword:
        where.append("(summary LIKE ? OR payload LIKE ?)")
        params += [f"%{keyword}%", f"%{keyword}%"]
    if level:
        where.append("level=?")
        params.append(level)
    with _db() as c:
        rows = c.execute(
            f"SELECT * FROM messages WHERE {' AND '.join(where)}"
            f" ORDER BY id DESC LIMIT ?",
            params + [limit],
        ).fetchall()
    return [dict(r) for r in rows]


def db_delete_bot(bot_id: str) -> None:
    with _db() as c:
        c.execute("DELETE FROM messages WHERE bot_id=?", (bot_id,))


def db_historical_bots() -> list[dict]:
    with _db() as c:
        rows = c.execute(
            "SELECT bot_id, bot_name, MAX(ts) last_seen, COUNT(*) cnt"
            " FROM messages GROUP BY bot_id"
        ).fetchall()
    return [dict(r) for r in rows]


# ─── Embedded HTTP Hub ────────────────────────────────────────────────────────

class _HubHandler(BaseHTTPRequestHandler):
    """Tiny HTTP server that receives bot messages and heartbeats."""

    def log_message(self, *_):
        pass   # suppress default access log to stdout

    def _respond(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    # ── GET / ─────────────────────────────────────────────────────────────────
    def do_GET(self) -> None:
        self._respond(200, {"service": "BotController Hub", "status": "running"})

    # ── POST /ingest  and  POST /heartbeat/{id} ───────────────────────────────
    def do_POST(self) -> None:
        try:
            body = self._read_body()
        except Exception:
            self._respond(400, {"error": "invalid JSON"})
            return

        # ── /ingest ───────────────────────────────────────────────────────────
        if self.path == "/ingest":
            bid   = str(body.get("bot_id",   "unknown"))
            bname = str(body.get("bot_name", bid))
            summ  = str(body.get("summary",  ""))
            lvl   = str(body.get("level",    "info"))
            pay   = body.get("payload")

            ts = db_insert(bid, bname, summ, lvl, pay)

            with _lock:
                if bid not in _bots:
                    _bots[bid] = _new_bot_record(bid, bname)
                b = _bots[bid]
                b["name"]      = bname
                b["status"]    = "online"
                b["last_seen"] = time.time()
                b["messages"].insert(0, {"ts": ts, "summary": summ, "level": lvl})
                if len(b["messages"]) > MAX_MEM:
                    b["messages"] = b["messages"][:MAX_MEM]

            self._respond(200, {"status": "ok"})

        # ── /heartbeat/{bot_id} ───────────────────────────────────────────────
        elif self.path.startswith("/heartbeat/"):
            bid   = self.path.split("/heartbeat/", 1)[1].strip("/")
            bname = str(body.get("bot_name", bid))
            stat  = str(body.get("status",   "online"))

            with _lock:
                if bid not in _bots:
                    _bots[bid] = _new_bot_record(bid, bname)
                b = _bots[bid]
                b["name"]      = bname
                b["status"]    = stat
                b["last_seen"] = time.time()

            self._respond(200, {"status": "ok"})

        else:
            self._respond(404, {"error": "endpoint not found"})

    # ── DELETE /bots/{bot_id} ─────────────────────────────────────────────────
    def do_DELETE(self) -> None:
        if self.path.startswith("/bots/"):
            bid = self.path.split("/bots/", 1)[1].strip("/")
            with _lock:
                _bots.pop(bid, None)
            db_delete_bot(bid)
            self._respond(200, {"status": "deleted", "bot_id": bid})
        else:
            self._respond(404, {"error": "not found"})

    def do_OPTIONS(self) -> None:
        self._respond(200, {})


def _start_hub() -> HTTPServer:
    srv = HTTPServer(("0.0.0.0", HUB_PORT), _HubHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ─── TTL watchdog ─────────────────────────────────────────────────────────────

def _ttl_watchdog() -> None:
    """Background thread: mark bots offline if they missed heartbeats."""
    while True:
        time.sleep(5)
        now = time.time()
        with _lock:
            for b in _bots.values():
                if b["status"] != "offline" and (now - b["last_seen"]) > BOT_TTL:
                    b["status"] = "offline"


# ─── Dashboard ────────────────────────────────────────────────────────────────

class Dashboard:
    """
    Tkinter-based desktop dashboard.
    Each attached bot gets its own resizable column.
    """

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self._procs:  dict[str, subprocess.Popen] = {}  # bot_id → process
        self._hidden: set[str] = set()

        # Control vars
        self._paused  = tk.BooleanVar(value=False)
        self._keyword = tk.StringVar(value="")
        self._level   = tk.StringVar(value="all")
        self._limit   = tk.IntVar(value=60)

        root.title("Nazgul")
        root.geometry("1400x860")
        root.configure(bg="#0f0f1a")
        root.minsize(800, 480)

        self._build_topbar()
        self._build_main_area()

        # Seed historical bots from DB (previous sessions)
        threading.Thread(target=self._seed_from_db, daemon=True).start()

        self._schedule_poll()

    # ── UI build ──────────────────────────────────────────────────────────────

    def _build_topbar(self) -> None:
        bar = tk.Frame(self.root, bg="#13132a", pady=6, padx=12)
        bar.pack(fill="x", side="top")

        tk.Label(bar, text="⚡ Nazgul",
                 bg="#13132a", fg="#e84560",
                 font=("Arial", 11, "bold")).pack(side="left", padx=(0, 14))

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)

        # Pause / Resume
        self._pause_btn = tk.Button(
            bar, text="⏸  Pause", command=self._toggle_pause,
            bg="#1e1e3a", fg="white", relief="flat",
            font=("Arial", 8, "bold"), padx=8, pady=2, cursor="hand2")
        self._pause_btn.pack(side="left", padx=4)

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)

        # Keyword filter
        tk.Label(bar, text="Filter:", bg="#13132a", fg="#777799",
                 font=("Arial", 8)).pack(side="left")
        tk.Entry(bar, textvariable=self._keyword, width=18,
                 bg="#0a0a1c", fg="white", insertbackground="white",
                 relief="flat", font=("Arial", 8)).pack(side="left", padx=(4, 10))

        # Level dropdown
        tk.Label(bar, text="Level:", bg="#13132a", fg="#777799",
                 font=("Arial", 8)).pack(side="left")
        lvl_opt = tk.OptionMenu(bar, self._level, "all", "info", "warning", "error")
        lvl_opt.config(bg="#1e1e3a", fg="white", relief="flat",
                       highlightthickness=0, activebackground="#2a2a4a",
                       font=("Arial", 8))
        lvl_opt["menu"].config(bg="#1e1e3a", fg="white")
        lvl_opt.pack(side="left", padx=(4, 10))

        # Message limit
        tk.Label(bar, text="Max:", bg="#13132a", fg="#777799",
                 font=("Arial", 8)).pack(side="left")
        tk.Entry(bar, textvariable=self._limit, width=4,
                 bg="#0a0a1c", fg="white", insertbackground="white",
                 relief="flat", font=("Arial", 8)).pack(side="left", padx=(4, 12))

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)

        # Attach Bot — prominent button
        tk.Button(bar, text="📎  Attach Bot", command=self._on_attach,
                  bg="#e84560", fg="white", relief="flat",
                  font=("Arial", 9, "bold"),
                  padx=12, pady=3, cursor="hand2").pack(side="left", padx=8)

        # Stats (right-aligned)
        self._stats_var = tk.StringVar(value="")
        tk.Label(bar, textvariable=self._stats_var,
                 bg="#13132a", fg="#445566", font=("Arial", 8)).pack(side="right")

    def _build_main_area(self) -> None:
        """Horizontal-scrollable canvas that holds bot columns side-by-side."""
        container = tk.Frame(self.root, bg="#0f0f1a")
        container.pack(fill="both", expand=True)

        h_scroll = ttk.Scrollbar(container, orient="horizontal")
        h_scroll.pack(side="bottom", fill="x")

        self._canvas = tk.Canvas(container, bg="#0f0f1a",
                                 highlightthickness=0,
                                 xscrollcommand=h_scroll.set)
        self._canvas.pack(fill="both", expand=True)
        h_scroll.config(command=self._canvas.xview)

        self._cols_frame = tk.Frame(self._canvas, bg="#0f0f1a")
        self._frame_id   = self._canvas.create_window(
            (0, 0), window=self._cols_frame, anchor="nw")

        self._cols_frame.bind(
            "<Configure>",
            lambda _e: self._canvas.configure(
                scrollregion=self._canvas.bbox("all")))
        self._canvas.bind(
            "<Configure>",
            lambda e: self._canvas.itemconfig(self._frame_id, height=e.height))

    # ── Bot management ────────────────────────────────────────────────────────

    def _on_attach(self) -> None:
        path = filedialog.askopenfilename(
            title="Select a bot script (.py)",
            filetypes=[("Python scripts", "*.py"), ("All files", "*.*")])
        if not path:
            return

        # Derive a stable bot_id from the filename
        base   = os.path.basename(path)
        bot_id = (os.path.splitext(base)[0]
                  .lower()
                  .replace("-", "_")
                  .replace(" ", "_"))

        # Guard: don't double-attach
        if bot_id in self._procs and self._procs[bot_id].poll() is None:
            messagebox.showinfo(
                "Already Attached",
                f"'{base}' is already running.\n"
                "Click  ✖ Detach & Remove  first, then attach again.")
            return

        python_exe = _system_python()

        try:
            proc = subprocess.Popen(
                [python_exe, path],
                cwd=os.path.dirname(path),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            # Give the script 2 s to either start properly or crash
            time.sleep(2.0)
            if proc.poll() is not None:
                out, _ = proc.communicate()
                messagebox.showerror(
                    "Bot Crashed Immediately",
                    f"'{base}' exited right after launch.\n\n"
                    f"── Output ──\n{(out or '')[:900]}")
                return

            self._procs[bot_id] = proc
            self._hidden.discard(bot_id)

            # Pre-register so the column appears without waiting for a message
            with _lock:
                if bot_id not in _bots:
                    _bots[bot_id] = _new_bot_record(bot_id, base)

        except FileNotFoundError:
            messagebox.showerror(
                "Python Not Found",
                f"Could not find a Python interpreter at:\n  {python_exe}\n\n"
                "Install Python 3 and make sure it is on your PATH.")
        except Exception as exc:
            messagebox.showerror("Error", f"Could not launch '{base}'.\n{exc}")

    def _on_detach(self, bot_id: str) -> None:
        """Kill process, wipe DB history, remove column."""
        proc = self._procs.pop(bot_id, None)
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=4)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        db_delete_bot(bot_id)
        with _lock:
            _bots.pop(bot_id, None)
        self._hidden.discard(bot_id)

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _seed_from_db(self) -> None:
        """Load bots that sent messages in a previous session (shown offline)."""
        for row in db_historical_bots():
            bid = row["bot_id"]
            with _lock:
                if bid not in _bots:
                    _bots[bid] = {
                        "name":      row["bot_name"] or bid,
                        "status":    "offline",
                        "last_seen": 0.0,
                        "messages":  [],
                    }

    def _toggle_pause(self) -> None:
        paused = not self._paused.get()
        self._paused.set(paused)
        self._pause_btn.config(
            text="▶  Resume" if paused else "⏸  Pause",
            bg="#444466"    if paused else "#1e1e3a")

    def _schedule_poll(self) -> None:
        self._redraw()
        self.root.after(POLL_MS, self._schedule_poll)

    def _redraw(self) -> None:
        if self._paused.get():
            return

        with _lock:
            snapshot = {bid: dict(data) for bid, data in _bots.items()}

        online = sum(1 for b in snapshot.values() if b["status"] == "online")
        self._stats_var.set(
            f"  {online} / {len(snapshot)} online   │   hub port {HUB_PORT}")

        # Destroy and rebuild all columns every cycle
        for w in self._cols_frame.winfo_children():
            w.destroy()

        visible = {bid: data for bid, data in snapshot.items()
                   if bid not in self._hidden}

        if not visible:
            tk.Label(
                self._cols_frame,
                text="No bots attached.\n\nClick  📎 Attach Bot  to get started.",
                bg="#0f0f1a", fg="#2a2a44", font=("Arial", 14)).pack(
                    expand=True, pady=120, padx=120)
            return

        kw  = self._keyword.get().strip().lower()
        lvl = self._level.get()
        lim = max(1, self._limit.get())

        for col_idx, (bot_id, data) in enumerate(
                sorted(visible.items(), key=lambda x: x[1]["name"].lower())):
            self._draw_column(col_idx, bot_id, data, kw, lvl, lim)

    def _draw_column(self, idx: int, bot_id: str, data: dict,
                     kw: str, lvl: str, lim: int) -> None:
        status  = data["status"]
        name    = data["name"]
        icon    = STATUS_ICON.get(status, "⚪")
        running = (bot_id in self._procs
                   and self._procs[bot_id].poll() is None)

        # Per-status palette
        hdr_bg  = {"online": "#0c2a1a", "degraded": "#2a1e08", "offline": "#1a0c0c"}
        stat_fg = {"online": "#33ff88", "degraded": "#ffcc22", "offline": "#ff4444"}

        # ── Column container ──────────────────────────────────────────────────
        col = tk.Frame(self._cols_frame, bg="#181828",
                       highlightbackground="#252545",
                       highlightthickness=1)
        col.grid(row=0, column=idx, sticky="nsew", padx=4, pady=8)
        self._cols_frame.grid_columnconfigure(idx, weight=1, minsize=COL_MIN_W)
        col.grid_rowconfigure(2, weight=1)   # message area expands

        # ── Header ────────────────────────────────────────────────────────────
        hdr = tk.Frame(col, bg=hdr_bg.get(status, "#181828"), pady=7, padx=10)
        hdr.grid(row=0, column=0, sticky="ew")
        col.grid_columnconfigure(0, weight=1)

        tk.Label(hdr, text=f"{icon}  {name}",
                 bg=hdr_bg.get(status, "#181828"), fg="white",
                 font=("Arial", 10, "bold"), anchor="w").pack(side="left")
        tk.Label(hdr, text=f"[{status.upper()}]",
                 bg=hdr_bg.get(status, "#181828"),
                 fg=stat_fg.get(status, "gray"),
                 font=("Arial", 8, "bold")).pack(side="right")

        # ── Sub-header ────────────────────────────────────────────────────────
        sub = tk.Frame(col, bg="#111122", padx=10, pady=3)
        sub.grid(row=1, column=0, sticky="ew")
        tk.Label(sub, text=f"id: {bot_id}",
                 bg="#111122", fg="#334455",
                 font=("Courier", 7)).pack(side="left")
        if running:
            tk.Label(sub, text="● live",
                     bg="#111122", fg="#229944",
                     font=("Arial", 7)).pack(side="right")

        # ── Message feed ──────────────────────────────────────────────────────
        feed = scrolledtext.ScrolledText(
            col,
            bg="#0a0a18", fg="#9aaabb",
            font=("Courier", 8),
            wrap="word",
            state="normal",
            relief="flat", bd=0,
            padx=8, pady=6,
        )
        feed.grid(row=2, column=0, sticky="nsew")

        feed.tag_configure("ts",      foreground="#2a3a4a")
        feed.tag_configure("info",    foreground="#4499dd")
        feed.tag_configure("warning", foreground="#ddaa33")
        feed.tag_configure("error",   foreground="#ee4444")
        feed.tag_configure("dim",     foreground="#334455")

        msgs = db_get_messages(
            bot_id, limit=lim,
            keyword=kw   if kw            else None,
            level=lvl    if lvl != "all"  else None)

        if msgs:
            # Newest at top (already DESC from DB)
            for m in msgs:
                ts_str = (m.get("ts") or m.get("timestamp") or "")[:19]\
                         .replace("T", " ")
                summ   = m.get("summary", "")
                ml     = m.get("level", "info")
                mi     = LEVEL_ICON.get(ml, "·")
                feed.insert("end", f" {ts_str}  ", "ts")
                feed.insert("end", f"{mi}  {summ}\n", ml)
        else:
            feed.insert("end", "  Waiting for messages…\n", "dim")

        feed.config(state="disabled")

        # ── Action buttons ────────────────────────────────────────────────────
        btn_bar = tk.Frame(col, bg="#111122", pady=5, padx=6)
        btn_bar.grid(row=3, column=0, sticky="ew")

        tk.Button(
            btn_bar, text="✖  Detach & Remove",
            command=lambda bid=bot_id: self._on_detach(bid),
            bg="#3a0a0a", fg="#ff8888",
            font=("Arial", 7), relief="flat",
            padx=6, pady=2, cursor="hand2",
        ).pack(side="left", padx=(0, 4))

        tk.Button(
            btn_bar, text="Hide",
            command=lambda bid=bot_id: self._hidden.add(bid),
            bg="#1e1e3a", fg="#7777aa",
            font=("Arial", 7), relief="flat",
            padx=6, pady=2, cursor="hand2",
        ).pack(side="left")

    # ── Shutdown ──────────────────────────────────────────────────────────────

    def on_close(self) -> None:
        """Terminate all attached bot processes before closing."""
        for proc in list(self._procs.values()):
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self.root.destroy()


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    db_init()
    _start_hub()
    threading.Thread(target=_ttl_watchdog, daemon=True).start()

    root = tk.Tk()
    app  = Dashboard(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
