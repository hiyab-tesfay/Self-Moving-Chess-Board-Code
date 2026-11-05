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
            "clients": 0, "occupied": False, "board": True, "online": True, "motion": {"state": "idle", "stage": None},
            "ended": None, "outcome": None, "result": None}
    base.update(extra)
    return base


RESETTING = {"state": "moving", "stage": "gantry", "current": "reset"}


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
        self.assertEqual(view.mode, "pairing")
        view = describe_online(snapshot(clients=1), SETTINGS)
        self.assertTrue(view.connected)
        self.assertEqual(view.title, "Player connected")
        self.assertEqual(view.mode, "pairing", "the address and code stay up until the player starts a game")
        view = describe_online(snapshot(clients=1, started=True), SETTINGS)
        self.assertEqual((view.title, view.mode), ("Player to move", "game"))

    def test_a_stale_lease_on_an_old_game_is_not_a_player(self):
        """Bug 1: another session's 1-hour lease used to put that old game on the screen when Online was tapped."""
        board = chess.Board()
        board.push_uci("e2e4")
        view = describe_online(snapshot(occupied=True, started=True, fen=board.fen(), lastMove="e2e4", turn="b"), SETTINGS)
        self.assertFalse(view.connected)
        self.assertEqual((view.title, view.mode), ("Online", "pairing"))
        self.assertIn("chess.example.org", view.detail)

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
            self.assertEqual((view.title, view.mode), (title, "game"), motion)

    def test_a_capture_waits_for_the_piece_to_be_taken_off(self):
        """The gantry does not take captured pieces off: the server waits, the screen shows Check."""
        remove = {"state": "waiting", "stage": "remove", "current": "d8d5", "jobId": "job-3",
                  "instructions": ["Take the black pawn off d5, then press Check."]}
        view = describe_online(snapshot(clients=1, started=True, turn="w", motion=remove), SETTINGS)
        self.assertEqual((view.title, view.detail, view.status, view.mode, view.check_job),
                         ("Remove the piece", "Take the black pawn off d5, then press Check.",
                          "Waiting for the piece to be removed…", "game", "job-3"))
        error = "d5 still has a piece on it. Take it off, then tap Check."
        view = describe_online(snapshot(clients=1, started=True, turn="w", motion=dict(remove, error=error)), SETTINGS)
        self.assertEqual((view.title, view.detail, view.status, view.mode, view.check_job),
                         ("Remove the piece", error, error, "game", "job-3"))
        view = describe_online(snapshot(clients=1, started=True, turn="w", motion=dict(remove, instructions=None, jobId=None)), SETTINGS)
        self.assertEqual((view.detail, view.check_job), ("Take the captured piece off the board, then tap Check.", None))
        # A capture that ends the game still waits for the piece before the gantry plays it.
        view = describe_online(snapshot(clients=1, started=True, gameOver=True, result="Stockfish wins by checkmate.",
                                        motion=remove), SETTINGS)
        self.assertEqual((view.title, view.check_job, view.mode), ("Remove the piece", "job-3", "game"))
        # Every other waiting or moving state is as before, without a Check.
        for motion in ({"state": "waiting", "instructions": ["Move the black pawn from e7 to e5."]},
                       {"state": "moving", "stage": "gantry", "current": "d8d5", "jobId": "job-3"}):
            view = describe_online(snapshot(clients=1, started=True, turn="w", motion=motion), SETTINGS)
            self.assertIsNone(view.check_job, motion)
        self.assertEqual(view.title, "Gantry moving d8d5")

    def test_game_over_while_the_pieces_go_home(self):
        board = chess.Board("7k/5Q2/6K1/8/8/8/8/8 w - - 0 1")
        board.push_uci("f7f8")   # mate
        over = dict(clients=1, started=True, gameOver=True, fen=board.fen(), lastMove="f7f8", turn="b",
                    outcome={"reason": "checkmate", "winner": "w", "result": "1-0"}, result="You win by checkmate!")
        view = describe_online(snapshot(motion=RESETTING, **over), SETTINGS)
        self.assertEqual((view.title, view.detail, view.status, view.mode),
                         ("Game over", "You win by checkmate!", "Putting the pieces back…", "resetting"))
        self.assertEqual(view.board.piece_at(chess.F8).symbol(), "Q", "the final position stays on the screen")
        self.assertEqual(view.last_move, chess.Move.from_uci("f7f8"))
        view = describe_online(snapshot(motion={"state": "moving", "stage": "camera", "current": "reset"}, **over), SETTINGS)
        self.assertEqual(view.status, "Checking the pieces…")
        # The ways a game ends on the server without the chess rules.
        for reason, result, title in (("timeout", "White wins on time.", "Out of time"),
                                      ("abandoned", "Game ended from the board.", "Game over"),
                                      ("resignation", "Black resigned.", "Game over")):
            view = describe_online(snapshot(clients=0, started=True, gameOver=True, motion=RESETTING, result=result,
                                            ended={"reason": reason, "winner": "w", "result": "1-0"},
                                            outcome={"reason": reason, "winner": "w", "result": "1-0"}), SETTINGS)
            self.assertEqual((view.title, view.detail, view.mode), (title, result, "resetting"), reason)
        # The mating move itself may still be on its way when the game is already over.
        view = describe_online(snapshot(motion={"state": "moving", "stage": "gantry", "current": "f7f8"}, **over), SETTINGS)
        self.assertEqual((view.title, view.mode), ("Gantry moving f7f8", "game"))

    def test_after_the_reset_the_pairing_screen_names_the_last_game(self):
        for clients in (0, 1):
            view = describe_online(snapshot(clients=clients, started=True, gameOver=True, result="White wins on time.",
                                            ended={"reason": "timeout", "winner": "w", "result": "1-0"},
                                            motion={"state": "idle", "stage": None}), SETTINGS)
            self.assertEqual((view.title, view.mode), ("Online", "pairing"), clients)
            self.assertTrue(view.detail.startswith("Last game: White wins on time."), view.detail)
            self.assertIn("chess.example.org", view.detail)
            self.assertIn("Last game: White wins on time.", view.status)
            self.assertEqual(view.connected, clients > 0)
        view = describe_online(snapshot(clients=1, started=True, gameOver=True, result="Game drawn.", board=False), SETTINGS)
        self.assertEqual(view.mode, "pairing", "a digital game has no pieces to put back: the pairing screen at once")

    def test_a_reset_that_stops_asks_for_the_starting_position_and_check_again(self):
        fault = {"state": "fault", "stage": None, "current": "reset", "error": "The gantry lost a step."}
        view = describe_online(snapshot(clients=1, started=True, gameOver=True, result="White wins on time.", motion=fault), SETTINGS)
        self.assertEqual((view.title, view.mode), ("Board stopped", "fix_setup"))
        self.assertIn("The gantry lost a step.", view.detail)
        self.assertIn("Check Again", view.detail)
        self.assertIn("Check Again", view.status)
        # The same when a Reset Board from the pairing screen stops; a fault during a game still goes to the website.
        view = describe_online(snapshot(clients=0, motion=fault), SETTINGS)
        self.assertEqual(view.mode, "fix_setup")
        view = describe_online(snapshot(clients=1, started=True, motion={"state": "fault", "error": "x"}), SETTINGS)
        self.assertEqual((view.title, view.mode), ("Board stopped", "game"))
        self.assertIn("confirm on the website", view.detail)

    def test_reset_board_from_the_pairing_screen(self):
        view = describe_online(snapshot(clients=0, motion=RESETTING), SETTINGS)
        self.assertEqual((view.title, view.status, view.mode), ("Resetting the board", "Putting the pieces back…", "resetting"))
        view = describe_online(snapshot(clients=1, motion=RESETTING), SETTINGS)
        self.assertEqual(view.mode, "resetting")


class StandInServer:
    """Answers /api/online like the real game server, and remembers what the screen said."""

    def __init__(self, token):
        self.calls = []
        self.bodies = []      # every request body, failed ones included
        self.fail_next = 0    # answer this many requests with HTTP 503 first
        self.extra = {}       # fields added to the snapshot (a game in progress, a reset under way…)
        calls, server_ref = self.calls, self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.headers.get("Authorization") != "Bearer " + token:
                    self.send_response(401); self.end_headers(); return
                server_ref.bodies.append(body)
                if server_ref.fail_next > 0:
                    server_ref.fail_next -= 1
                    self.send_response(503); self.end_headers(); return
                calls.append((self.path, self.headers.get("X-Player-Session"), body.get("active")))
                reply = json.dumps(snapshot(clients=server_ref.clients, online=body.get("active") is True,
                                            **server_ref.extra)).encode()
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
        self.assertEqual(self.server.bodies[-1], {"active": True}, "a plain heartbeat carries only `active`")
        client.heartbeat(True, fresh=True)
        self.assertEqual(self.server.bodies[-1], {"active": True, "fresh": True})
        client.heartbeat(True, reset=True)
        self.assertEqual(self.server.bodies[-1], {"active": True, "reset": True})
        client.heartbeat(False)
        self.assertEqual(self.server.bodies[-1], {"active": False})
        with self.assertRaises(OnlineError):
            RelayClient(OnlineSettings("w" * 64, self.server.url, "x", None)).heartbeat()
        with self.assertRaises(OnlineError):
            RelayClient(OnlineSettings("t" * 64, "http://127.0.0.1:1", "x", None)).heartbeat()

    def test_confirm_posts_the_job_with_the_heartbeat_headers(self):
        client = RelayClient(self.settings)
        self.assertTrue(client.confirm("job-3")["ok"], "the answer is a snapshot like the heartbeat's")
        self.assertEqual(self.server.calls[-1][:2], ("/api/board-confirm", client.session))
        self.assertEqual(self.server.bodies[-1], {"jobId": "job-3"})
        with self.assertRaises(OnlineError):
            RelayClient(OnlineSettings("w" * 64, self.server.url, "x", None)).confirm("job-3")
        with self.assertRaises(OnlineError):
            RelayClient(OnlineSettings("t" * 64, "http://127.0.0.1:1", "x", None)).confirm("job-3")

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

    def wait_for_bodies(self, count, seconds=6.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and len(self.server.bodies) < count:
            time.sleep(0.02)
        return list(self.server.bodies)

    def test_the_first_heartbeat_after_the_camera_check_is_fresh_exactly_once(self):
        results = queue.SimpleQueue()
        session = OnlineSession(self.settings, results, generation=1, client=RelayClient(self.settings))
        session.start()
        bodies = self.wait_for_bodies(3)
        session.stop()
        session.thread.join(timeout=5)
        self.assertEqual(bodies[0], {"active": True, "fresh": True}, "the server drops the old game and lease")
        self.assertEqual(bodies[1:3], [{"active": True}, {"active": True}], "and `fresh` is not repeated")

    def test_fresh_is_retried_until_a_heartbeat_got_through(self):
        self.server.fail_next = 2
        results = queue.SimpleQueue()
        session = OnlineSession(self.settings, results, generation=1, client=RelayClient(self.settings))
        session.start()
        bodies = self.wait_for_bodies(4, seconds=10)
        session.stop()
        session.thread.join(timeout=5)
        self.assertEqual([b.get("fresh") for b in bodies[:4]], [True, True, True, None],
                         "two failed attempts, the first success, then plain heartbeats")
        _, _, value, error = results.get(timeout=1)
        self.assertIsInstance(error, OnlineError, "each failed attempt reaches the screen as an error")

    def test_end_game_or_reset_board_sends_reset_once_at_the_next_heartbeat(self):
        results = queue.SimpleQueue()
        session = OnlineSession(self.settings, results, generation=1, client=RelayClient(self.settings))
        session.start()
        self.wait_for_bodies(2)
        session.request_reset()
        self.assertTrue(session.reset_pending)
        bodies = self.wait_for_bodies(len(self.server.bodies) + 2)
        session.stop()
        session.thread.join(timeout=5)
        with_reset = [b for b in bodies if b.get("reset")]
        self.assertEqual(with_reset, [{"active": True, "reset": True}], "exactly one heartbeat carries `reset`")
        self.assertFalse(session.reset_pending)
        self.assertNotIn("fresh", with_reset[0], "`fresh` was already delivered")

    def test_the_screen_follows_the_server_through_the_reset(self):
        """A reset under way, then the pairing screen with the last game, as the server reports them."""
        self.server.clients = 1
        self.server.extra = dict(started=True, gameOver=True, result="Game ended from the board.", motion=RESETTING,
                                 ended={"reason": "abandoned", "winner": None, "result": "*"})
        results = queue.SimpleQueue()
        session = OnlineSession(self.settings, results, generation=1, client=RelayClient(self.settings))
        session.start()
        _, _, value, _ = results.get(timeout=5)
        view = describe_online(value, self.settings)
        self.assertEqual((view.title, view.status, view.mode), ("Game over", "Putting the pieces back…", "resetting"))
        self.server.extra["motion"] = {"state": "idle", "stage": None}
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and view.mode != "pairing":
            _, _, value, _ = results.get(timeout=5)
            view = describe_online(value, self.settings)
        session.stop()
        session.thread.join(timeout=5)
        self.assertEqual(view.mode, "pairing")
        self.assertIn("Last game: Game ended from the board.", view.detail)


if __name__ == "__main__":
    unittest.main()
