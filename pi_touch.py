"""Touch screen front end for the camera verified Raspberry Pi chess game."""

from __future__ import annotations

import argparse
import base64
import queue
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import chess
import cv2
import numpy as np

from board_vision import (VisionError, board_corners, calibrate, load_calibration,
                          observe, read_image, save_calibration)
from chess_game import ChessGame, DIFFICULTIES, StockfishPlayer
from move_observation import match_legal_moves, occupied_squares
from pi_camera import Camera
from pi_game import capture_observation, verifies_move


class MoveRejected(ValueError):
    """The physical board cannot safely advance to the next position."""


class CalibrationFlow:
    """Touch calibration steps; photos are processed off the Tk main thread."""

    CORNER_NAMES = ("top-left", "top-right", "bottom-right", "bottom-left")

    def __init__(self) -> None:
        self.stage = "empty"
        self.empty_photo: np.ndarray | None = None
        self.uses_markers = False
        self.corners: list[list[float]] = []

    def accept_empty(self, photo: np.ndarray, uses_markers: bool) -> None:
        if self.stage != "empty":
            raise ValueError("The empty board was already photographed.")
        self.empty_photo = photo
        self.uses_markers = uses_markers
        self.corners = []
        self.stage = "starting" if uses_markers else "corners"

    def add_corner(self, x: float, y: float) -> None:
        if self.stage != "corners" or len(self.corners) == 4:
            raise ValueError("Board corners are not being selected.")
        self.corners.append([x, y])
        if len(self.corners) == 4:
            self.stage = "starting"

    def undo_corner(self) -> None:
        if self.stage not in ("corners", "starting") or self.uses_markers or not self.corners:
            return
        self.corners.pop()
        self.stage = "corners"

    def reset_corners(self) -> None:
        if not self.uses_markers and self.empty_photo is not None:
            self.corners = []
            self.stage = "corners"


def calibrate_photos(empty: np.ndarray, starting: np.ndarray,
                     corners: list[list[float]] | None):
    """Build and verify calibration before writing it to disk."""
    calibration, reference, _scores = calibrate(empty, starting, corners)
    observation, _aligned = observe(reference, starting, calibration, corners)
    if observation.uncertain or observation.occupied != occupied_squares(chess.Board()):
        raise VisionError("Starting board could not be verified. Check the pieces, corners, and lighting.")
    return calibration, reference


class TouchSession:
    """Rules and camera match decisions, independent of the display and hardware."""

    def __init__(self, level: str) -> None:
        self.game = ChessGame()
        self.game.start(level)
        self.phase = "setup"
        self.choices: tuple[chess.Move, ...] = ()
        self.computer_move: chess.Move | None = None
        self.result: str | None = None

    def verify_setup(self, occupied: frozenset[str]) -> None:
        if self.phase != "setup":
            raise ValueError("Setup is not waiting for a scan.")
        if occupied != occupied_squares(self.game.board):
            raise MoveRejected("Board does not match the starting position. Correct the pieces and scan again.")
        self.phase = "human"

    def scan_human(self, occupied: frozenset[str]) -> str | None:
        if self.phase != "human":
            raise ValueError("It is not your turn to scan a move.")
        match = match_legal_moves(self.game.board, occupied)
        if match.status == "unchanged":
            raise MoveRejected("No move detected. Move a white piece, then scan again.")
        if match.status == "no_match":
            raise MoveRejected("Illegal or unrecognized move. Correct the board and scan again.")
        if match.status == "ambiguous":
            self.choices = match.candidates
            self.phase = "choose"
            return None
        return self._accept_human(match.candidates[0])

    def select_move(self, move: chess.Move) -> str:
        if self.phase != "choose" or move not in self.choices:
            raise ValueError("Select one of the moves shown on screen.")
        self.choices = ()
        return self._accept_human(move)

    def retake(self) -> None:
        if self.phase != "choose":
            raise ValueError("There is no move selection to retake.")
        self.choices = ()
        self.phase = "human"

    def _accept_human(self, move: chess.Move) -> str:
        san = self.game.play_human(
            chess.square_name(move.from_square),
            chess.square_name(move.to_square),
            chess.piece_symbol(move.promotion) if move.promotion else None,
        )
        self.result = self.game.result_text()
        self.phase = "finished" if self.result else "thinking"
        return san

    def announce_computer(self, move: chess.Move) -> str:
        if self.phase != "thinking" or move not in self.game.board.legal_moves:
            raise ValueError("The computer did not provide a legal move.")
        self.computer_move = move
        self.phase = "computer"
        return self.game.board.san(move)

    def verify_computer(self, occupied: frozenset[str]) -> str:
        if self.phase != "computer" or self.computer_move is None:
            raise ValueError("There is no computer move to verify.")
        if not verifies_move(self.game.board, self.computer_move, occupied):
            raise MoveRejected("Board does not match the computer move. Correct it and scan again.")
        san = self.game.play_computer(self.computer_move)
        self.computer_move = None
        self.result = self.game.result_text()
        self.phase = "finished" if self.result else "human"
        return san

    def resign(self) -> None:
        if self.phase != "finished":
            self.result = "You ended the game. Stockfish wins."
            self.phase = "finished"


class TouchApp:
    BG = "#101820"
    PANEL = "#192733"
    TEXT = "#f1f5f7"
    MUTED = "#bdcbd3"
    GREEN = "#77db9c"
    RED = "#ff8c85"
    GOLD = "#ffcf70"

    def __init__(self, root: tk.Tk, args: argparse.Namespace) -> None:
        self.root = root
        self.args = args
        self.camera = Camera(args.camera, args.device)
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.results: queue.SimpleQueue = queue.SimpleQueue()
        self.generation = 0
        self.closed = False
        self.player: StockfishPlayer | None = None
        self.calibration = None
        self.reference = None
        self.session: TouchSession | None = None
        self.calibration_flow: CalibrationFlow | None = CalibrationFlow()
        self.startup_calibration = True
        self.preview_photo: tk.PhotoImage | None = None
        self.preview_geometry: tuple[float, float, float] | None = None
        self.level = "beginner"
        self.busy = False
        self.selected_square: str | None = None
        self.message = "Calibrate the board, or use a saved calibration."
        self.message_color = self.MUTED
        screen_width = 800 if args.windowed else root.winfo_screenwidth()
        screen_height = 480 if args.windowed else root.winfo_screenheight()
        self.compact = screen_width < 700 or screen_height < 450
        self.panel_width = 235 if self.compact else 320

        root.title("Physical Chess")
        root.configure(bg=self.BG)
        if args.windowed:
            root.geometry("800x480")
            root.minsize(800, 480)
        else:
            root.attributes("-fullscreen", True)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Escape>", lambda _event: self.close())

        self.header = tk.Label(root, text="PHYSICAL CHESS", bg=self.BG, fg=self.TEXT,
                               font=("DejaVu Sans", 13 if self.compact else 18, "bold"),
                               anchor="w", padx=12 if self.compact else 18)
        self.header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.board_canvas = tk.Canvas(root, bg=self.BG, highlightthickness=0)
        self.board_canvas.grid(row=1, column=0, sticky="nsew", padx=(12, 4), pady=8)
        self.board_canvas.bind("<Configure>", lambda _event: self.draw_board())
        self.board_canvas.bind("<Button-1>", self.tap_board)
        self.panel = tk.Frame(root, bg=self.PANEL, width=self.panel_width,
                              padx=8 if self.compact else 16, pady=5 if self.compact else 12)
        self.panel.grid(row=1, column=1, sticky="nsew", padx=(2, 6 if self.compact else 12), pady=5 if self.compact else 8)
        self.panel.grid_propagate(False)
        self.status = tk.Label(root, text=self.message, bg=self.PANEL, fg=self.message_color,
                               font=("DejaVu Sans", 10 if self.compact else 13, "bold"),
                               anchor="w", justify="left", wraplength=screen_width - 36,
                               padx=8 if self.compact else 16, pady=4 if self.compact else 8)
        self.status.grid(row=2, column=0, columnspan=2, sticky="nsew",
                         padx=6 if self.compact else 12, pady=(0, 4 if self.compact else 10))
        root.grid_rowconfigure(1, weight=1)
        root.grid_columnconfigure(0, weight=1)
        root.grid_columnconfigure(1, minsize=self.panel_width + (10 if self.compact else 20))
        self.render()
        root.after(70, self.poll_results)

    def say(self, message: str, color: str | None = None) -> None:
        self.message = message
        self.message_color = color or self.MUTED
        self.status.configure(text=message, fg=self.message_color)

    def button_widget(self, parent, label: str, command, *,
                      accent: bool = False, short: bool = False) -> tk.Button:
        return tk.Button(parent, text=label, command=command,
                         height=1 if self.compact or short else 2,
                         font=("DejaVu Sans", 10 if self.compact else 12, "bold"),
                         bd=0, relief="flat",
                         bg=self.GREEN if accent else "#314554",
                         fg=self.BG if accent else self.TEXT,
                         activebackground=self.GOLD, activeforeground=self.BG,
                         wraplength=self.panel_width - 30)

    def button(self, label: str, command, *, accent: bool = False,
               short: bool = False) -> None:
        self.button_widget(self.panel, label, command, accent=accent, short=short).pack(
            fill="x", pady=2 if self.compact else 4,
        )

    def heading(self, title: str, detail: str = "") -> None:
        tk.Label(self.panel, text=title, bg=self.PANEL, fg=self.TEXT,
                 font=("DejaVu Sans", 12 if self.compact else 16, "bold"),
                 anchor="w").pack(fill="x", pady=(0, 3 if self.compact else 8))
        if detail and not self.compact:
            tk.Label(self.panel, text=detail, bg=self.PANEL, fg=self.MUTED,
                     font=("DejaVu Sans", 11), anchor="w", justify="left",
                     wraplength=self.panel_width - 35).pack(fill="x", pady=(0, 12))

    def render(self) -> None:
        for child in self.panel.winfo_children():
            child.destroy()
        self.draw_board()
        if self.busy:
            self.heading("Please wait", "The camera or chess engine is working.")
            self.button("Cancel Calibration" if self.calibration_flow else "End Game",
                        self.cancel_calibration if self.calibration_flow else self.end_game)
            return
        if self.calibration_flow is not None:
            self.render_calibration()
            return
        session = self.session
        if session is None:
            self.heading("New game", "Play White on the physical board. Tap a piece on the screen to see legal destinations.")
            for key, difficulty in DIFFICULTIES.items():
                label = ("● " if key == self.level else "○ ") + difficulty.label
                self.button(label, lambda selected=key: self.choose_level(selected), short=True)
            self.button("Start Game", self.start_game, accent=True)
            self.button("Calibrate Board", self.start_calibration, short=True)
        elif session.phase == "setup":
            self.heading("Set up the board", "Place all 32 pieces in the starting position, then scan.")
            self.button("Scan Starting Board", self.scan, accent=True)
            self.button("Cancel Game", self.end_game)
        elif session.phase == "human":
            self.heading("Your move · White", "Move a piece on the physical board, then tap Scan Move. Tap a white piece on screen for legal squares.")
            self.button("Scan Move", self.scan, accent=True)
            self.button("End Game", self.end_game)
        elif session.phase == "choose":
            promotion = all(move.promotion for move in session.choices)
            self.heading("Choose promotion" if promotion else "Choose your move",
                         "The camera sees the same occupied squares for these legal moves.")
            choices_frame = tk.Frame(self.panel, bg=self.PANEL)
            choices_frame.pack(fill="x", pady=(0, 4))
            choices_frame.grid_columnconfigure(0, weight=1)
            choices_frame.grid_columnconfigure(1, weight=1)
            for index, move in enumerate(session.choices):
                name = chess.piece_name(move.promotion).title() if move.promotion else session.game.board.san(move)
                label = name if self.compact else f"{name}  ({move.uci()})"
                choice_button = self.button_widget(
                    choices_frame, label, lambda chosen=move: self.select_move(chosen),
                )
                choice_button.configure(wraplength=(self.panel_width - 35) // 2,
                                        font=("DejaVu Sans", 10, "bold"))
                choice_button.grid(row=index // 2, column=index % 2,
                                   sticky="ew", padx=2, pady=2)
            self.button("Retake Photo", self.retake)
            self.button("End Game", self.end_game)
        elif session.phase == "thinking":
            self.heading("Computer thinking", "Leave the physical board as it is.")
            self.button("Try Computer Move", self.request_computer_move, accent=True)
            self.button("End Game", self.end_game)
        elif session.phase == "computer":
            move = session.computer_move
            assert move is not None
            san = session.game.board.san(move)
            extra = []
            if session.game.board.is_capture(move):
                extra.append("Remove the captured piece.")
            if session.game.board.is_castling(move):
                extra.append("Move the rook too.")
            if move.promotion:
                extra.append(f"Replace the pawn with a {chess.piece_name(move.promotion)}.")
            self.heading(f"Stockfish: {san}", f"Move {move.uci()[:2]} to {move.uci()[2:4]} on the board. " + " ".join(extra))
            self.button("Verify Computer Move", self.scan, accent=True)
            self.button("End Game", self.end_game)
        else:
            self.heading("Game over", session.result or "Game finished.")
            self.button("New Game", self.back_to_menu, accent=True)

    def choose_level(self, level: str) -> None:
        self.level = level
        self.say(f"{DIFFICULTIES[level].label} selected.")
        self.render()

    def render_calibration(self) -> None:
        flow = self.calibration_flow
        assert flow is not None
        if flow.stage == "empty":
            self.heading("1. Empty board", "Remove every piece. Keep the whole board in the camera view.")
            self.button("Take Empty Photo", self.capture_empty, accent=True)
            if self.saved_calibration_available():
                self.button("Use Saved Calibration", self.use_saved_calibration)
            if not self.startup_calibration:
                self.button("Back to Menu", self.cancel_calibration)
        elif flow.stage == "corners":
            next_corner = flow.CORNER_NAMES[len(flow.corners)]
            self.heading("2. Mark corners", f"Tap the outer {next_corner} corner on the photo. Keep the camera fixed.")
            self.button("Undo Corner", self.undo_corner)
            self.button("Retake Empty Photo", self.retake_empty)
            self.button("Cancel Calibration", self.cancel_calibration)
        elif flow.stage == "starting":
            self.heading("3. Starting pieces", "Place all 32 pieces normally. White pieces should be at the bottom of the photo.")
            self.button("Take Setup Photo", self.capture_starting, accent=True)
            if not flow.uses_markers and not self.args.corners:
                self.button("Redo Corners", self.redo_corners)
            self.button("Cancel Calibration", self.cancel_calibration)
        else:
            self.heading("Calibration saved", "The starting board was verified. You can now play.")
            self.button("Choose Difficulty", self.continue_after_calibration, accent=True)

    def saved_calibration_available(self) -> bool:
        return ((self.args.calibration / "calibration.json").is_file() and
                (self.args.calibration / "empty_warp.png").is_file())

    def use_saved_calibration(self) -> None:
        if self.calibration_flow is None or self.busy or not self.saved_calibration_available():
            return
        self.busy = True
        self.say("Checking saved calibration…")
        self.render()
        self.submit("saved_calibration", lambda: load_calibration(self.args.calibration))

    def start_calibration(self) -> None:
        if self.session is not None or self.busy:
            return
        self.generation += 1
        self.calibration_flow = CalibrationFlow()
        self.preview_photo = None
        self.preview_geometry = None
        self.say("Remove all pieces, then tap Take Empty Photo.")
        self.render()

    def cancel_calibration(self) -> None:
        self.generation += 1
        self.busy = False
        self.calibration_flow = CalibrationFlow() if self.startup_calibration else None
        self.preview_photo = None
        self.preview_geometry = None
        self.say("Calibrate the board, or use a saved calibration." if self.startup_calibration else
                 "Choose a difficulty or recalibrate the board.")
        self.render()

    def retake_empty(self) -> None:
        self.calibration_flow = CalibrationFlow()
        self.preview_photo = None
        self.preview_geometry = None
        self.say("Remove all pieces and retake the empty-board photo.")
        self.render()

    def undo_corner(self) -> None:
        flow = self.calibration_flow
        assert flow is not None
        flow.undo_corner()
        self.say(f"Tap the outer {flow.CORNER_NAMES[len(flow.corners)]} corner.")
        self.render()

    def redo_corners(self) -> None:
        flow = self.calibration_flow
        assert flow is not None
        flow.reset_corners()
        self.say("Tap the outer top-left corner, then continue clockwise.")
        self.render()

    def capture_empty(self) -> None:
        flow = self.calibration_flow
        if flow is None or flow.stage != "empty" or self.busy:
            return
        self.busy = True
        self.say("Taking the empty-board photo…")
        self.render()

        def take_photo():
            path = self.args.calibration / "empty.jpg"
            self.camera.capture(path)
            photo = read_image(path)
            if self.args.corners:
                return photo, False
            try:
                board_corners(photo)
                return photo, True
            except VisionError:
                return photo, False

        self.submit("cal_empty", take_photo)

    def capture_starting(self) -> None:
        flow = self.calibration_flow
        if flow is None or flow.stage != "starting" or flow.empty_photo is None or self.busy:
            return
        empty = flow.empty_photo
        corners = None if flow.uses_markers else [point.copy() for point in flow.corners]
        self.busy = True
        self.say("Taking the starting-board photo and checking calibration…")
        self.render()

        def take_photo():
            path = self.args.calibration / "starting.jpg"
            self.camera.capture(path)
            starting = read_image(path)
            return calibrate_photos(empty, starting, corners)

        self.submit("cal_starting", take_photo)

    def continue_after_calibration(self) -> None:
        self.startup_calibration = False
        self.calibration_flow = None
        self.preview_photo = None
        self.preview_geometry = None
        self.say("Calibration is ready. Choose a difficulty to begin.", self.GREEN)
        self.render()

    def submit(self, tag: str, work) -> None:
        generation = self.generation

        def run() -> None:
            try:
                value = work()
                if tag == "start" and (self.closed or generation != self.generation):
                    value[2].close()
                    return
                self.results.put((generation, tag, value, None))
            except Exception as exc:
                self.results.put((generation, tag, None, exc))

        self.executor.submit(run)

    def start_game(self) -> None:
        self.generation += 1
        self.busy = True
        self.say("Loading calibration and starting Stockfish…")
        self.render()

        def load():
            calibration, reference = load_calibration(self.args.calibration)
            return calibration, reference, StockfishPlayer()

        self.submit("start", load)

    def scan(self) -> None:
        if self.session is None or self.busy:
            return
        self.busy = True
        self.say("Taking a photo and checking the physical board…")
        self.render()
        self.submit("scan", lambda: capture_observation(
            self.camera, self.args.calibration / "captures", self.calibration,
            self.reference, self.args.corners,
        ))

    def poll_results(self) -> None:
        if self.closed:
            return
        try:
            while True:
                generation, tag, value, error = self.results.get_nowait()
                if generation != self.generation:
                    if tag == "start" and value is not None:
                        self.executor.submit(value[2].close)
                    continue
                self.busy = False
                if error is not None:
                    if tag == "start" and isinstance(error, VisionError):
                        self.say("Calibration missing or unreadable. Tap Calibrate Board.", self.RED)
                    else:
                        self.say(f"Error: {error}", self.RED)
                    self.render()
                    continue
                try:
                    self.handle_result(tag, value)
                except (ValueError, RuntimeError, OSError, cv2.error) as exc:
                    self.say(str(exc), self.RED)
                self.render()
        except queue.Empty:
            pass
        self.root.after(70, self.poll_results)

    def handle_result(self, tag: str, value) -> None:
        if tag == "saved_calibration":
            self.calibration, self.reference = value
            self.startup_calibration = False
            self.calibration_flow = None
            self.say("Saved calibration loaded. Choose a difficulty to begin.", self.GREEN)
        elif tag == "cal_empty":
            flow = self.calibration_flow
            assert flow is not None
            photo, uses_markers = value
            flow.accept_empty(photo, uses_markers)
            if self.args.corners:
                flow.corners = [point.copy() for point in self.args.corners]
                flow.stage = "starting"
            if self.args.corners:
                self.say("Saved corner coordinates are ready. Place the starting pieces and take the next photo.")
            elif uses_markers:
                self.say("Markers found. Place the starting pieces and take the next photo.")
            else:
                self.say("Tap the outer top-left corner of the board photo, then continue clockwise.")
        elif tag == "cal_starting":
            flow = self.calibration_flow
            assert flow is not None
            calibration, reference = value
            save_calibration(self.args.calibration, calibration, reference)
            self.calibration, self.reference = calibration, reference
            flow.stage = "done"
            self.say("Calibration saved and starting board verified. Tap Choose Difficulty.", self.GREEN)
        elif tag == "start":
            self.calibration, self.reference, self.player = value
            self.session = TouchSession(self.level)
            self.say("Place the pieces in their starting squares and scan the board.")
        elif tag == "scan":
            assert self.session is not None
            if self.session.phase == "setup":
                self.session.verify_setup(value)
                self.say("Starting position verified. Your move is legal when the scan matches a chess move.", self.GREEN)
            elif self.session.phase == "human":
                san = self.session.scan_human(value)
                if san is None:
                    self.say("Legal move detected. Choose the piece or move shown on screen.", self.GREEN)
                else:
                    self.say(f"Legal move: {san}", self.GREEN)
                    self.after_human_move()
            elif self.session.phase == "computer":
                san = self.session.verify_computer(value)
                self.say(f"Computer move {san} verified. " +
                         (self.session.result or "Your turn."), self.GREEN)
        elif tag == "engine":
            assert self.session is not None
            san = self.session.announce_computer(value)
            directions = f"Stockfish: {san}. Move {value.uci()[:2]} to {value.uci()[2:4]}."
            if self.session.game.board.is_capture(value):
                directions += " Remove the captured piece."
            if self.session.game.board.is_castling(value):
                directions += " Move the rook too."
            if value.promotion:
                directions += f" Use a {chess.piece_name(value.promotion)}."
            self.say(directions, self.GOLD)

    def after_human_move(self) -> None:
        assert self.session is not None
        self.selected_square = None
        if self.session.phase == "finished":
            self.say(self.session.result or "Game over.", self.GREEN)
            return
        assert self.player is not None
        self.request_computer_move()

    def request_computer_move(self) -> None:
        if self.session is None or self.session.phase != "thinking" or self.busy:
            return
        assert self.player is not None
        board = self.session.game.board.copy(stack=True)
        level = self.session.game.level
        player = self.player
        self.busy = True
        self.submit("engine", lambda: player.choose_move(board, level))
        self.render()

    def select_move(self, move: chess.Move) -> None:
        assert self.session is not None
        try:
            san = self.session.select_move(move)
            self.say(f"Legal move: {san}", self.GREEN)
            self.after_human_move()
        except ValueError as exc:
            self.say(str(exc), self.RED)
        self.render()

    def retake(self) -> None:
        assert self.session is not None
        self.session.retake()
        self.say("Retake the board photo after your move.")
        self.render()

    def end_game(self) -> None:
        self.generation += 1
        self.busy = False
        if self.player is not None:
            player, self.player = self.player, None
            self.executor.submit(player.close)
        if self.session is None or self.session.phase == "setup":
            self.session = None
            self.say("Game cancelled.")
        else:
            self.session.resign()
            self.say(self.session.result or "Game ended.", self.GOLD)
        self.render()

    def back_to_menu(self) -> None:
        self.generation += 1
        if self.player is not None:
            player, self.player = self.player, None
            self.executor.submit(player.close)
        self.session = None
        self.selected_square = None
        self.say("Choose a difficulty to begin.")
        self.render()

    def tap_board(self, event) -> None:
        if self.calibration_flow is not None:
            self.tap_calibration_corner(event)
            return
        if self.session is None or self.session.phase != "human" or self.busy:
            return
        x, y, size, square_size = self.board_geometry()
        if not (x <= event.x < x + size and y <= event.y < y + size):
            return
        col = int((event.x - x) / square_size)
        row = int((event.y - y) / square_size)
        if not (0 <= col < 8 and 0 <= row < 8):
            return
        name = f"{'abcdefgh'[col]}{8 - row}"
        legal = self.session.game.legal_destinations(name)
        self.selected_square = name if legal else None
        self.say(f"{name}: legal destinations {', '.join(sorted(legal))}." if legal else
                 f"{name}: choose one of your pieces.", self.GOLD if legal else self.MUTED)
        self.draw_board()

    def tap_calibration_corner(self, event) -> None:
        flow = self.calibration_flow
        if flow is None or flow.stage != "corners" or flow.empty_photo is None or self.busy:
            return
        if self.preview_geometry is None:
            return
        left, top, scale = self.preview_geometry
        height, width = flow.empty_photo.shape[:2]
        x = (event.x - left) / scale
        y = (event.y - top) / scale
        if not (0 <= x < width and 0 <= y < height):
            return
        flow.add_corner(x, y)
        if flow.stage == "starting":
            self.say("Corners set. Keep the camera fixed, place all starting pieces, then take the setup photo.")
        else:
            self.say(f"Tap the outer {flow.CORNER_NAMES[len(flow.corners)]} corner.")
        self.render()

    def draw_calibration_photo(self) -> None:
        flow = self.calibration_flow
        assert flow is not None and flow.empty_photo is not None
        canvas = self.board_canvas
        canvas.delete("all")
        photo = flow.empty_photo
        height, width = photo.shape[:2]
        available_width = canvas.winfo_width() if canvas.winfo_width() > 1 else 400
        available_height = canvas.winfo_height() if canvas.winfo_height() > 1 else 360
        scale = min((available_width - 20) / width, (available_height - 20) / height)
        display_width = max(1, round(width * scale))
        display_height = max(1, round(height * scale))
        resized = cv2.resize(photo, (display_width, display_height), interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode(".png", resized)
        if not ok:
            raise VisionError("Could not display the empty-board photo.")
        self.preview_photo = tk.PhotoImage(data=base64.b64encode(encoded.tobytes()).decode("ascii"))
        left = (available_width - display_width) / 2
        top = (available_height - display_height) / 2
        canvas.create_image(left, top, image=self.preview_photo, anchor="nw")
        self.preview_geometry = (left, top, scale)
        for index, (x, y) in enumerate(flow.corners):
            px, py = left + x * scale, top + y * scale
            canvas.create_oval(px - 7, py - 7, px + 7, py + 7,
                               fill=self.GOLD, outline=self.BG, width=2)
            canvas.create_text(px + 12, py - 12, text=str(index + 1), fill=self.GOLD,
                               font=("DejaVu Sans", 12, "bold"))

    def board_geometry(self) -> tuple[float, float, float, float]:
        actual_width = self.board_canvas.winfo_width()
        actual_height = self.board_canvas.winfo_height()
        width = actual_width if actual_width > 1 else 400
        height = actual_height if actual_height > 1 else 360
        size = max(80, min(width - 34, height - 30))
        square_size = size / 8
        return (width - size) / 2, (height - size) / 2, size, square_size

    def draw_board(self) -> None:
        if (self.calibration_flow is not None and
                self.calibration_flow.stage in ("corners", "starting") and
                self.calibration_flow.empty_photo is not None):
            self.draw_calibration_photo()
            return
        canvas = self.board_canvas
        canvas.delete("all")
        self.preview_geometry = None
        board = self.session.game.board if self.session else chess.Board()
        x, y, size, step = self.board_geometry()
        highlights: set[str] = set()
        if self.session and self.session.phase == "human" and self.selected_square:
            highlights = self.session.game.legal_destinations(self.selected_square)
        last_move = self.session.game.last_move if self.session else None
        pending = self.session.computer_move if self.session else None
        for row in range(8):
            for col in range(8):
                square = chess.square(col, 7 - row)
                name = chess.square_name(square)
                left, top = x + col * step, y + row * step
                fill = "#e8ddc4" if (row + col) % 2 == 0 else "#678577"
                if last_move and square in (last_move.from_square, last_move.to_square):
                    fill = "#c6ae65"
                if pending and square in (pending.from_square, pending.to_square):
                    fill = "#e3b76d"
                if name == self.selected_square:
                    fill = "#ffe08a"
                canvas.create_rectangle(left, top, left + step, top + step,
                                        fill=fill, outline=fill)
                if name in highlights:
                    margin = step * .36
                    canvas.create_oval(left + margin, top + margin,
                                       left + step - margin, top + step - margin,
                                       fill="#ffd66e", outline="")
                piece = board.piece_at(square)
                if piece:
                    radius = step * .35
                    cx, cy = left + step / 2, top + step / 2
                    canvas.create_oval(cx - radius, cy - radius, cx + radius, cy + radius,
                                       fill="#faf6ea" if piece.color else "#243541",
                                       outline="#354550", width=2)
                    canvas.create_text(cx, cy, text=piece.symbol().upper(),
                                       fill="#202c32" if piece.color else "#ffffff",
                                       font=("DejaVu Sans", max(16, int(step * .46)), "bold"))
        for col, file in enumerate("abcdefgh"):
            canvas.create_text(x + (col + .5) * step, y + size + 10, text=file,
                               fill=self.MUTED, font=("DejaVu Sans", 9))

    def close(self) -> None:
        self.closed = True
        self.generation += 1
        try:
            while True:
                _generation, tag, value, _error = self.results.get_nowait()
                if tag == "start" and value is not None:
                    self.executor.submit(value[2].close)
        except queue.Empty:
            pass
        if self.player is not None:
            player, self.player = self.player, None
            self.executor.submit(player.close)
        self.executor.shutdown(wait=False)
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration", type=Path, default=Path("calibration"))
    parser.add_argument("--camera", choices=("pi", "usb"), default="pi")
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--corners", nargs=8, type=float,
                        metavar=("TL_X", "TL_Y", "TR_X", "TR_Y", "BR_X", "BR_Y", "BL_X", "BL_Y"))
    parser.add_argument("--windowed", action="store_true", help="Show an 800×480 window for development")
    args = parser.parse_args(argv)
    if args.corners:
        args.corners = [args.corners[index:index + 2] for index in range(0, 8, 2)]
    root = tk.Tk()
    TouchApp(root, args)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
