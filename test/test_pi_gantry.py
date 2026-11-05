"""The gantry bridge against the pretend board: a short game with a capture, the layout after every
move, and the reset. Needs the gantry folder (CHESS_GANTRY_DIR, or one of the default places) and
a C++ compiler for the pretend board; skips otherwise.

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


def pretend_board_builds() -> bool:
    if GANTRY_DIR is None or not shutil.which("make") or not shutil.which("c++"):
        return False
    host = GANTRY_DIR.parent / "firmware" / "host"
    return subprocess.run(["make", "-s", "-C", str(host), "virtual_gantry"], capture_output=True).returncode == 0


class FromEnvTests(unittest.TestCase):
    def test_no_port_means_no_gantry(self):
        with mock.patch.dict(os.environ, {"CHESS_GANTRY_PORT": ""}):
            self.assertIsNone(pi_gantry.GantryPlayer.from_env())

    def test_a_port_without_a_folder_means_no_gantry_and_no_crash(self):
        with mock.patch.dict(os.environ, {"CHESS_GANTRY_PORT": "sim", "CHESS_GANTRY_DIR": "/nowhere/gantry/pi"}):
            self.assertIsNone(pi_gantry.GantryPlayer.from_env())


@unittest.skipUnless(pretend_board_builds(), "needs the gantry folder and a C++ compiler for the pretend board")
class PretendBoardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["CHESS_GANTRY_PORT"] = "sim"
        os.environ["CHESS_GANTRY_DIR"] = str(GANTRY_DIR)
        os.environ.pop("CHESS_GANTRY_CONFIG", None)
        os.environ["GANTRY_SIM_SPEED"] = "200"

    def test_a_short_game_with_a_capture_then_the_reset(self):
        player = pi_gantry.GantryPlayer.from_env()
        self.assertIsNotNone(player, "CHESS_GANTRY_PORT=sim and a gantry folder give a player")
        said = []
        try:
            player.connect()
            player.new_game()
            board = chess.Board()
            self.assertEqual(player.expected_squares(), set(occupied_squares(board)))
            # White is the person, Black is the gantry. Nxe5 by the person takes a pawn off the board;
            # Nxe5 by the gantry docks the white knight first, then carries the black knight.
            for uci in ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "f8c5", "f3e5", "c6e5"]:
                move = chess.Move.from_uci(uci)
                if board.turn == chess.WHITE:
                    player.human_moved(board, move)
                else:
                    notes = player.play(board, move, say=said.append)
                    self.assertEqual(notes, [], f"nothing left for the person after {uci}")
                board.push(move)
                self.assertEqual(player.expected_squares(), set(occupied_squares(board)), f"after {uci}")
            self.assertEqual(said[-2:], ["Taking the white knight off e5 to slot w:0", "Moving the black knight from c6 to e5"])
            self.assertEqual(len(said), 5, "one message per piece carried: e7e5, b8c6, f8c5, and the capture's two")
            self.assertEqual(said[0], "Moving the black pawn from e7 to e5")
            self.assertEqual(player.layout.pieces["w:0"], "N", "the taken knight waits in White's lane")
            self.assertNotIn("b:0", player.layout.pieces, "the pawn the person took is simply gone")

            notes = player.reset(say=said.append)
            self.assertEqual(player.expected_squares(), set(occupied_squares(chess.Board())) - {"e7"}, "everything home but the pawn the person kept")
            self.assertEqual(notes, ["Put a black pawn on e7."])
            self.assertTrue(any(text.startswith("Bringing the white knight from slot w:0 to g1") for text in said), said)
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
