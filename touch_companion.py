"""A laptop Confirm button for the Pi touchscreen's single game session.

HTTP threads only queue actions. The Tk thread owns the camera, game and gantry.
Listen on loopback; a laptop reaches this through the existing SSH tunnel.
"""
from __future__ import annotations

import json
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import queue
import threading
from urllib.parse import urlsplit
from manual_controls import valid_manual

ASSETS = Path(__file__).resolve().parent / "frontend/camera-game"


class CompanionBridge:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.actions: queue.Queue = queue.Queue(maxsize=1)
        self.stop = threading.Event()
        self.pending = False
        self.revision = 0
        self.state: dict = {}
        self.photo = b""

    def publish_photo(self, photo: bytes) -> str:
        with self.lock:
            self.photo = photo
        return hashlib.sha256(photo).hexdigest()[:16]

    def read_photo(self) -> bytes:
        with self.lock:
            return self.photo

    def publish(self, state: dict) -> None:
        copy = json.loads(json.dumps(state))
        with self.lock:
            if copy != self.state:
                self.state = copy
                self.revision += 1

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state, revision=self.revision, pending=self.pending)

    def submit(self, action: str, revision: int, uci: str | int | None = None) -> bool:
        if action == "stop":
            self.stop.set()
            return True
        with self.lock:
            if revision != self.revision or self.pending or self.state.get("busy"):
                return False
            if action == "confirm":
                allowed = self.state.get("can_confirm", False)
            elif action == "choice":
                allowed = uci in {choice["uci"] for choice in self.state.get("choices", [])}
            elif action == "rating":
                settings = self.state.get("difficulty") or {}
                allowed = (settings.get("enabled", False) and type(uci) is int and
                           settings["min"] <= uci <= settings["max"])
            elif action == "control":
                allowed = isinstance(uci, str) and uci in {item["id"] for item in self.state.get("controls", [])}
            elif action == "corner":
                allowed = (bool((self.state.get("photo") or {}).get("clickable")) and
                           isinstance(uci, list) and len(uci) == 2 and
                           all(type(value) in (int, float) and 0 <= value < 1 for value in uci))
            elif action == "manual":
                allowed = bool(self.state.get("manual_enabled")) and valid_manual(uci)
            else:
                allowed = False
            if not allowed:
                return False
            self.pending = True
            self.actions.put_nowait((action, revision, uci))
            return True

    def next_action(self):
        try:
            return self.actions.get_nowait()
        except queue.Empty:
            return None

    def complete_action(self) -> None:
        with self.lock:
            self.pending = False


class CompanionServer:
    def __init__(self, port: int = 8789) -> None:
        self.bridge = CompanionBridge()
        bridge = self.bridge

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, status: int, body: bytes, kind: str = "application/json") -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; object-src 'none'")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if urlsplit("http://" + self.headers.get("Host", "")).hostname not in ("localhost", "127.0.0.1", "::1"):
                    self.reply(403, b'{}')
                    return
                if self.path == "/api/state":
                    self.reply(200, json.dumps(bridge.snapshot()).encode())
                    return
                if urlsplit(self.path).path == "/api/photo":
                    photo = bridge.read_photo()
                    self.reply(200 if photo else 404, photo, "image/png")
                    return
                files = {"/": ("index.html", "text/html; charset=utf-8"),
                         "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                         "/style.css": ("style.css", "text/css; charset=utf-8")}
                asset = files.get(self.path)
                if asset is None:
                    self.reply(404, b'{}')
                    return
                self.reply(200, (ASSETS / asset[0]).read_bytes(), asset[1])

            def do_POST(self):
                # JSON + same-origin checks prevent another website from triggering a carry.
                origin = self.headers.get("Origin")
                if (urlsplit("http://" + self.headers.get("Host", "")).hostname not in ("localhost", "127.0.0.1", "::1") or
                        self.headers.get_content_type() != "application/json" or
                        (origin and urlsplit(origin).netloc != self.headers.get("Host"))):
                    self.reply(403, b'{"error":"Use the camera chess page to confirm a move."}')
                    return
                if self.path not in ("/api/confirm", "/api/choice", "/api/stop", "/api/rating", "/api/control", "/api/corner", "/api/manual"):
                    self.reply(404, b'{}')
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 1024:
                        raise ValueError("Invalid request size")
                    body = json.loads(self.rfile.read(size))
                    accepted = bridge.submit(self.path.rsplit("/", 1)[1],
                                             int(body.get("revision", -1)),
                                             body.get("rating") if self.path == "/api/rating" else
                                             body.get("value") if self.path in ("/api/control", "/api/corner", "/api/manual") else body.get("uci"))
                except (ValueError, TypeError, AttributeError):
                    self.reply(400, b'{"error":"Invalid confirmation."}')
                    return
                self.reply(202 if accepted else 409, json.dumps({"accepted": accepted}).encode())

        self.http = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.http.daemon_threads = True
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)
