"""A laptop Confirm button for the Pi touchscreen's single game session.

HTTP threads only queue actions. The Tk thread owns the camera, game and gantry.
Listen on loopback; a laptop reaches this through the existing SSH tunnel.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import queue
import threading
from urllib.parse import urlsplit

ASSETS = Path(__file__).resolve().parent / "frontend/camera-game"


class CompanionBridge:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.actions: queue.Queue = queue.Queue(maxsize=1)
        self.stop = threading.Event()
        self.pending = False
        self.revision = 0
        self.state: dict = {}

    def publish(self, state: dict) -> None:
        copy = json.loads(json.dumps(state))
        with self.lock:
            if copy != self.state:
                self.state = copy
                self.revision += 1

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state, revision=self.revision, pending=self.pending)

    def submit(self, action: str, revision: int, uci: str | None = None) -> bool:
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
                if self.path not in ("/api/confirm", "/api/choice", "/api/stop"):
                    self.reply(404, b'{}')
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 1024:
                        raise ValueError("Invalid request size")
                    body = json.loads(self.rfile.read(size))
                    accepted = bridge.submit(self.path.rsplit("/", 1)[1],
                                             int(body.get("revision", -1)), body.get("uci"))
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
