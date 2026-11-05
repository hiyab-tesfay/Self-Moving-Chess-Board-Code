"""Online mode without a Pi: settings, the heartbeat against a stand-in game server, and what the screen shows."""
import json
import queue
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import chess

from move_observation import occupied_squares
from pi_online import OnlineError, OnlineSession, OnlineSettings, RelayClient, describe_online, read_env_file, starting_position_problem

SETTINGS = OnlineSettings("t" * 64, "http://127.0.0.1:1", "https://chess.example.org", "ABCD")


def snapshot(**extra):
    base = {"ok": True, "started": False, "fen": chess.Board().fen(), "lastMove": None, "turn": "w", "gameOver": False,
            "clients": 0, "occupied": False, "board": True, "online": True, "motion": {"state": "idle", "stage": None}}
    base.update(extra)
    return base


class SettingsTest(unittest.TestCase):
    def test_env_file_parsing_and_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "relay.env"
            path.write_text('# comment\nexport CHESS_RELAY_TOKEN="' + "x" * 40 + '"\nCHESS_STATE_PATH=/tmp/x\nCHESS_ACCESS_CODE=\'1234\'\n')
            env = read_env_file(path)
            self.assertEqual(env["CHESS_RELAY_TOKEN"], "x" * 40)
            self.assertEqual(env["CHESS_ACCESS_CODE"], "1234")
            settings = OnlineSettings.load(path)
            self.assertEqual((settings.relay_url, settings.website_display, settings.code),
                             ("http://127.0.0.1:8765", "chess-website-gules.vercel.app", "1234"))
            path.write_text("CHESS_STATE_PATH=/tmp/x\n")
            with self.assertRaises(OnlineError):
                OnlineSettings.load(path)
            with self.assertRaises(OnlineError):
                OnlineSettings.load(Path(directory) / "missing.env")


class StartingPositionTest(unittest.TestCase):
    def test_the_website_is_let_in_only_when_all_32_pieces_are_in_place(self):
        start = occupied_squares(chess.Board())
        self.assertIsNone(starting_position_problem(start))
        self.assertIsNone(starting_position_problem(start, uncertain=("d4",)), "an unclear empty square does not matter")
        problem = starting_position_problem((start - {"e2"}) | {"e4"})
        self.assertIn("missing on e2", problem)
        self.assertIn("extra on e4", problem)
        self.assertIn("Check Again", problem)
        self.assertIsNone(starting_position_problem(start - {"e2"}, uncertain=("e2",)), "an unclear square is given the benefit of the doubt")
        self.assertIn("cannot tell", starting_position_problem(start, uncertain=("a1", "b1", "c1")))
        self.assertIn("missing on", starting_position_problem(frozenset()))


class ViewTest(unittest.TestCase):
    def test_pairing_until_a_browser_is_connected(self):
        view = describe_online(snapshot(), SETTINGS)
        self.assertFalse(view.connected)
        self.assertIn("chess.example.org", view.detail)
        self.assertIn("Waiting for a player", view.status)
        view = describe_online(snapshot(clients=1), SETTINGS)
        self.assertTrue(view.connected)
        self.assertEqual(view.title, "Player connected")

    def test_the_game_as_the_website_plays_it(self):
        board = chess.Board()
        board.push_uci("e2e4")
        view = describe_online(snapshot(clients=1, started=True, fen=board.fen(), lastMove="e2e4", turn="b",
                                        motion={"state": "moving", "stage": "gantry", "current": "e2e4"}), SETTINGS)
        self.assertEqual(view.title, "Gantry moving e2e4")
        self.assertEqual(view.board.piece_at(chess.E4).symbol(), "P")
        self.assertEqual(view.last_move, chess.Move.from_uci("e2e4"))
        for motion, title in (({"state": "moving", "stage": "stockfish"}, "Stockfish thinking"),
                              ({"state": "moving", "stage": "camera", "current": "e7e5"}, "Camera checking e7e5"),
                              ({"state": "fault", "error": "The camera does not see the board the game expects."}, "Board stopped"),
                              ({"state": "waiting", "instructions": ["Move the black pawn from e7 to e5."]}, "Move the piece by hand"),
                              ({"state": "idle"}, "Player to move")):
            view = describe_online(snapshot(clients=1, started=True, turn="w", motion=motion), SETTINGS)
            self.assertEqual(view.title, title, motion)
        view = describe_online(snapshot(clients=1, started=True, gameOver=True, result="You win by checkmate!"), SETTINGS)
        self.assertEqual((view.title, view.detail), ("Game over", "You win by checkmate!"))

    def test_a_player_holding_the_board_counts_as_connected_even_between_polls(self):
        self.assertTrue(describe_online(snapshot(occupied=True, started=True), SETTINGS).connected)


class StandInServer:
    """Answers /api/online like the real game server, and remembers what the screen said."""

    def __init__(self, token):
        self.calls = []
        calls, server_ref = self.calls, self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.headers.get("Authorization") != "Bearer " + token:
                    self.send_response(401); self.end_headers(); return
                calls.append((self.path, self.headers.get("X-Player-Session"), body.get("active")))
                reply = json.dumps(snapshot(clients=server_ref.clients, online=body.get("active") is True)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args):
                pass

        self.clients = 0
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class HeartbeatTest(unittest.TestCase):
    def setUp(self):
        self.server = StandInServer("t" * 64)
        self.settings = OnlineSettings("t" * 64, self.server.url, "https://chess.example.org", None)

    def tearDown(self):
        self.server.close()

    def test_heartbeat_uses_the_token_and_a_screen_session(self):
        client = RelayClient(self.settings)
        self.assertTrue(client.session.startswith("touchscreen-"))
        self.assertTrue(client.heartbeat(True)["online"])
        self.assertEqual(self.server.calls[-1], ("/api/online", client.session, True))
        with self.assertRaises(OnlineError):
            RelayClient(OnlineSettings("w" * 64, self.server.url, "x", None)).heartbeat()
        with self.assertRaises(OnlineError):
            RelayClient(OnlineSettings("t" * 64, "http://127.0.0.1:1", "x", None)).heartbeat()

    def test_the_session_thread_heartbeats_until_stopped_then_says_goodbye(self):
        results = queue.SimpleQueue()
        session = OnlineSession(self.settings, results, generation=7, client=RelayClient(self.settings))
        session.start()
        generation, tag, value, error = results.get(timeout=5)
        self.assertEqual((generation, tag, error), (7, "online", None))
        self.assertFalse(describe_online(value, self.settings).connected)
        self.server.clients = 1
        deadline = time.monotonic() + 5
        connected = False
        while time.monotonic() < deadline and not connected:
            _, _, value, _ = results.get(timeout=5)
            connected = describe_online(value, self.settings).connected
        self.assertTrue(connected, "a browser showing up turns the pairing screen into the game")
        session.stop()
        session.thread.join(timeout=5)
        self.assertEqual(self.server.calls[-1][2], False, "leaving Online tells the server at once")


if __name__ == "__main__":
    unittest.main()
