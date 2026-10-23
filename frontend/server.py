#!/usr/bin/env python3
"""Serve the web frontend and expose chess_game.ChessGame + Stockfish as a small JSON API.

Run from the repository root (or anywhere):

    python3 frontend/server.py            # http://127.0.0.1:8000
    python3 frontend/server.py --port 8080 --host 0.0.0.0

The browser UI talks to these endpoints (all JSON):

    GET  /api/status                 current game state, levels, engine availability
    POST /api/new    {"level": ...}  start a new game; the human plays White
    POST /api/move   {"from": "e2", "to": "e4", "promotion": "q"|null}
                                     play the human move, then Stockfish replies
    POST /api/undo                   take back the last human move (and Stockfish's reply)
    GET  /api/legal?from=e2          legal destination squares for a white piece

Only the Python standard library plus the project's existing dependencies are used.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

FRONTEND_DIR = Path(__file__).resolve().parent
REPO_ROOT = FRONTEND_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import chess  # noqa: E402
from chess_game import DIFFICULTIES, ChessGame, PromotionRequired, StockfishPlayer  # noqa: E402


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


class GameService:
    """One shared game, guarded by a lock, backed by chess_game.ChessGame."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.game = ChessGame()
        self.player: StockfishPlayer | None = None
        self.engine_error: str | None = None
        self._connect_engine()

    def _connect_engine(self) -> None:
        try:
            self.player = StockfishPlayer()
            self.engine_error = None
        except Exception as exc:  # Stockfish missing or broken: keep serving, report it
            self.player = None
            self.engine_error = str(exc)

    def state(self, **extra) -> dict:
        board = self.game.board
        last = self.game.last_move
        payload = {
            "ok": True,
            "started": self.game.started,
            "level": self.game.level,
            "levels": [
                {"id": key, "label": d.label, "description": d.description}
                for key, d in DIFFICULTIES.items()
            ],
            "fen": board.fen(),
            "turn": "w" if board.turn == chess.WHITE else "b",
            "check": board.is_check(),
            "gameOver": board.is_game_over(claim_draw=True),
            "result": self.game.result_text(),
            "sanMoves": list(self.game.san_moves),
            "lastMove": last.uci() if last else None,
            "revision": self.game.revision,
            "engine": self.player is not None,
            "engineError": self.engine_error,
        }
        payload.update(extra)
        return payload

    def new_game(self, level: str) -> dict:
        with self.lock:
            try:
                self.game.start(level)
            except ValueError as exc:
                raise ApiError(400, str(exc))
            if self.player is None:
                self._connect_engine()
            return self.state()

    def legal(self, square: str) -> dict:
        with self.lock:
            return {"ok": True, "from": square, "destinations": sorted(self.game.legal_destinations(square))}

    def move(self, source: str, target: str, promotion: str | None) -> dict:
        with self.lock:
            try:
                human_san = self.game.play_human(source, target, promotion)
            except PromotionRequired as exc:
                raise ApiError(409, str(exc), promotionRequired=True)
            except ValueError as exc:
                raise ApiError(400, str(exc))
            human = {"uci": self.game.last_move.uci(), "san": human_san}

            computer = None
            if not self.game.board.is_game_over(claim_draw=True):
                if self.player is None:
                    self._connect_engine()
                if self.player is None:
                    raise ApiError(503, self.engine_error or "Stockfish is not available.",
                                   human=human, **self.state())
                try:
                    reply = self.player.choose_move(self.game.board.copy(stack=True), self.game.level)
                    computer_san = self.game.play_computer(reply)
                except Exception as exc:
                    raise ApiError(502, f"Stockfish error: {exc}", human=human, **self.state())
                computer = {"uci": reply.uci(), "san": computer_san}
            return self.state(human=human, computer=computer)

    def undo(self) -> dict:
        with self.lock:
            board = self.game.board
            if not self.game.started or not board.move_stack:
                raise ApiError(400, "Nothing to take back.")
            # Take back Stockfish's reply and the human move together when it is the human's turn.
            plies = 2 if board.turn == chess.WHITE and len(board.move_stack) >= 2 else 1
            for _ in range(plies):
                board.pop()
                if self.game.san_moves:
                    self.game.san_moves.pop()
            self.game.last_move = board.peek() if board.move_stack else None
            self.game.revision += 1
            return self.state(undone=plies)

    def close(self) -> None:
        if self.player is not None:
            try:
                self.player.close()
            except Exception:
                pass


SERVICE: GameService | None = None


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, directory=str(FRONTEND_DIR), **kwargs)

    # ---- helpers -------------------------------------------------------

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "Request body must be JSON.")
        return data if isinstance(data, dict) else {}

    def _api(self, method: str, path: str, query: dict) -> None:
        assert SERVICE is not None
        try:
            if method == "GET" and path == "/api/status":
                self._send_json(200, SERVICE.state())
            elif method == "GET" and path == "/api/legal":
                square = (query.get("from") or [""])[0]
                self._send_json(200, SERVICE.legal(square))
            elif method == "POST" and path == "/api/new":
                body = self._read_json()
                self._send_json(200, SERVICE.new_game(str(body.get("level") or "beginner")))
            elif method == "POST" and path == "/api/move":
                body = self._read_json()
                promotion = body.get("promotion")
                self._send_json(200, SERVICE.move(
                    str(body.get("from") or ""), str(body.get("to") or ""),
                    str(promotion).lower() if promotion else None))
            elif method == "POST" and path == "/api/undo":
                self._send_json(200, SERVICE.undo())
            else:
                raise ApiError(404, f"Unknown API route: {method} {path}")
        except ApiError as exc:
            self._send_json(exc.status, {"ok": False, "error": exc.message, **exc.extra})
        except Exception as exc:  # keep the server alive on unexpected errors
            self._send_json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    # ---- HTTP verbs ----------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._api("GET", parsed.path, parse_qs(parsed.query))
        else:
            super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._api("POST", parsed.path, parse_qs(parsed.query))
        else:
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

    def end_headers(self) -> None:
        if not urlparse(self.path).path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main() -> None:
    global SERVICE
    parser = argparse.ArgumentParser(description="Serve the chess web frontend with chess_game + Stockfish.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    SERVICE = GameService()
    if SERVICE.player is None:
        print(f"Warning: {SERVICE.engine_error}", file=sys.stderr)
        print("The board will load, but computer moves need Stockfish.", file=sys.stderr)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Chess frontend: http://{args.host}:{args.port}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        SERVICE.close()


if __name__ == "__main__":
    main()
