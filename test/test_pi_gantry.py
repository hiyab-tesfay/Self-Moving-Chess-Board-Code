"""The gantry bridge against the pretend board: a short game with a capture (the person takes the
piece off, the gantry makes one carry), the layout after every move, and the reset. Needs the gantry
folder (CHESS_GANTRY_DIR, or one of the default places) and a C++ compiler for the pretend board;
skips otherwise.

    CHESS_GANTRY_DIR=.../gantry/pi .venv/bin/python -m unittest test.test_pi_gantry
"""
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest import mock

import chess

import pi_gantry
from move_observation import occupied_squares

GANTRY_DIR = pi_gantry.find_gantry_dir(os.environ.get("CHESS_GANTRY_DIR"))
GAME = ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "f8c5", "f3e5", "c6e5"]   # White is the person, Black the gantry


def pretend_board_builds() -> bool:
    if GANTRY_DIR is None or not shutil.which("make") or not shutil.which("c++"):
        return False
    host = GANTRY_DIR.parent / "firmware" / "host"
    return subprocess.run(["make", "-s", "-C", str(host), "virtual_gantry"], capture_output=True).returncode == 0


def sim_environment() -> None:
    os.environ["CHESS_GANTRY_PORT"] = "sim"
    os.environ["CHESS_GANTRY_DIR"] = str(GANTRY_DIR)
    os.environ.pop("CHESS_GANTRY_CONFIG", None)
    os.environ["GANTRY_SIM_SPEED"] = "200"


class FromEnvTests(unittest.TestCase):
    def test_no_port_means_no_gantry(self):
        with mock.patch.dict(os.environ, {"CHESS_GANTRY_PORT": ""}):
            self.assertIsNone(pi_gantry.GantryPlayer.from_env())

    def test_a_port_without_a_folder_means_no_gantry_and_no_crash(self):
        with mock.patch.dict(os.environ, {"CHESS_GANTRY_PORT": "sim", "CHESS_GANTRY_DIR": "/nowhere/gantry/pi"}):
            self.assertIsNone(pi_gantry.GantryPlayer.from_env())

    def test_auto_port_without_a_device_means_no_gantry_and_a_warning(self):
        with mock.patch.dict(os.environ, {"CHESS_GANTRY_PORT": "auto"}), \
                mock.patch.object(pi_gantry.glob, "glob", return_value=[]), \
                self.assertLogs("pi_gantry", level="WARNING") as logs:
            self.assertIsNone(pi_gantry.GantryPlayer.from_env())
        self.assertIn("auto", "\n".join(logs.output))


class AutoPortTests(unittest.TestCase):
    def fake_devices(self, devices):
        import fnmatch
        return mock.patch.object(pi_gantry.glob, "glob",
                                 side_effect=lambda pattern: [d for d in devices if fnmatch.fnmatch(d, pattern)])

    def test_usb_first_sorted_then_acm(self):
        with self.fake_devices(["/dev/ttyACM0", "/dev/ttyUSB1", "/dev/ttyUSB0"]):
            self.assertEqual(pi_gantry.find_serial_port(), "/dev/ttyUSB0")
        with self.fake_devices(["/dev/ttyACM1", "/dev/ttyACM0"]):
            self.assertEqual(pi_gantry.find_serial_port(), "/dev/ttyACM0")
        with self.fake_devices([]):
            self.assertIsNone(pi_gantry.find_serial_port())

    def test_auto_port_picks_the_device_for_the_player(self):
        created = []
        with mock.patch.dict(os.environ, {"CHESS_GANTRY_PORT": "auto", "CHESS_GANTRY_DIR": "/some/gantry/pi",
                                          "CHESS_GANTRY_CONFIG": "/some/gantry_config.json"}), \
                self.fake_devices(["/dev/ttyACM0", "/dev/ttyUSB0"]), \
                mock.patch.object(pi_gantry, "find_gantry_dir", return_value=Path("/some/gantry/pi")), \
                mock.patch.object(Path, "is_file", return_value=True), \
                mock.patch.object(pi_gantry.GantryPlayer, "__init__",
                                  lambda self, directory, port, config: created.append(port) or None):
            self.assertIsNotNone(pi_gantry.GantryPlayer.from_env())
        self.assertEqual(created, ["/dev/ttyUSB0"])


@unittest.skipUnless(GANTRY_DIR, "needs the gantry folder (CHESS_GANTRY_DIR)")
class PlanningTests(unittest.TestCase):
    """What the bridge works out without touching a board."""

    @classmethod
    def setUpClass(cls):
        sim_environment()

    def test_removal_needed_only_for_a_capture(self):
        player = pi_gantry.GantryPlayer.from_env()
        board = chess.Board()
        needed = {}
        for uci in GAME:
            move = chess.Move.from_uci(uci)
            needed[uci] = player.removal_needed(board, move)
            board.push(move)
        self.assertEqual(needed, {**{uci: None for uci in GAME}, "f3e5": ("e5", "p"), "c6e5": ("e5", "N")})
        board = chess.Board()
        for uci in ["e2e4", "a7a6", "e4e5", "d7d5"]:
            board.push_uci(uci)
        self.assertEqual(player.removal_needed(board, chess.Move.from_uci("e5d6")), ("d5", "p"), "en passant: the passed pawn's square")
        self.assertIsNone(player.link, "nothing was opened")


@unittest.skipUnless(pretend_board_builds(), "needs the gantry folder and a C++ compiler for the pretend board")
class PretendBoardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sim_environment()

    def test_a_short_game_with_a_capture_then_the_reset(self):
        player = pi_gantry.GantryPlayer.from_env()
        self.assertIsNotNone(player, "CHESS_GANTRY_PORT=sim and a gantry folder give a player")
        said = []
        try:
            player.connect()
            player.new_game()
            board = chess.Board()
            self.assertEqual(player.expected_squares(), set(occupied_squares(board)))
            # Nxe5 by the person takes a pawn off the board. Nxe5 by the gantry: the person takes the white
            # knight off e5 (the screen has camera-checked that), then the gantry carries the black knight there.
            for uci in GAME:
                move = chess.Move.from_uci(uci)
                if board.turn == chess.WHITE:
                    player.human_moved(board, move)
                else:
                    taken = player.removal_needed(board, move)
                    self.assertEqual(taken, ("e5", "N") if uci == "c6e5" else None, uci)
                    notes = player.play(board, move, say=said.append)
                    self.assertEqual(notes, [], f"nothing left for the person after {uci}")
                board.push(move)
                self.assertEqual(player.expected_squares(), set(occupied_squares(board)), f"after {uci}")
            self.assertEqual(said, ["Moving the black pawn from e7 to e5", "Moving the black knight from b8 to c6",
                                    "Moving the black bishop from f8 to c5", "Moving the black knight from c6 to e5"],
                             "one carry per gantry move; the capture is a single carry onto the emptied square")
            self.assertEqual([n for n in player.layout.pieces if ":" in n], [], "nothing waits in a lane")
            self.assertEqual(len(player.layout.pieces), 30, "the two taken pieces are with the person")

            notes = player.reset(say=said.append)
            self.assertEqual(notes, ["Put a black pawn on e7.", "Put a white knight on g1."], "what the gantry cannot bring back")
            self.assertEqual(player.expected_squares(), set(occupied_squares(chess.Board())),
                             "the camera check expects the full start, so it only passes once the person has put them back")
            # The planner may carry the pieces home in any order: check the same four carries, once each.
            self.assertCountEqual(said[4:], ["Moving the white bishop from c4 to f1", "Moving the black bishop from c5 to f8",
                                             "Moving the white pawn from e4 to e2", "Moving the black knight from e5 to b8"])
        finally:
            player.close()

    def test_an_illegal_move_is_a_plain_error_and_the_layout_stands(self):
        player = pi_gantry.GantryPlayer.from_env()
        try:
            player.new_game()
            with self.assertRaises(pi_gantry.GantryError) as caught:
                player.play(chess.Board(), chess.Move.from_uci("e2e5"))
            self.assertIn("e2e5", str(caught.exception))
            self.assertEqual(player.expected_squares(), set(occupied_squares(chess.Board())))
            self.assertIsNone(player.link, "nothing was opened for a move that could not be planned")
        finally:
            player.close()


if __name__ == "__main__":
    unittest.main()
