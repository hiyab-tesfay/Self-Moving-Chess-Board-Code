"""Pure-Python Tkinter simulator for playing against a local Stockfish engine."""

from __future__ import annotations

import queue
import sys
import tkinter as tk
from concurrent.futures import Future, ThreadPoolExecutor
from tkinter import messagebox

import chess

from backend.chess_game import ChessGame, DIFFICULTIES, PromotionRequired, StockfishPlayer


PIECES = {
    "P": "♙", "N": "♘", "B": "♗", "R": "♖", "Q": "♕", "K": "♔",
    "p": "♟", "n": "♞", "b": "♝", "r": "♜", "q": "♛", "k": "♚",
}
LIGHT = "#efe9da"
DARK = "#77907c"
INK = "#26352c"
CREAM = "#f4f0e7"
PAPER = "#fffdf8"
ACCENT = "#a95f41"


class ChessSimulator:
    def __init__(self, root: tk.Tk, player: StockfishPlayer) -> None:
        self.root = root
        self.player = player
        self.game = ChessGame()
        self.selected: str | None = None
        self.thinking = False
        self.closing = False
        self.responses: queue.Queue[tuple[int, Future[chess.Move]]] = queue.Queue()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stockfish")
        self.level_choice = tk.StringVar(value="beginner")
        self.status = tk.StringVar(value="Choose a level and start a game.")
        self.note = tk.StringVar(value="You play White and move first.")
        self.squares: dict[str, tk.Button] = {}
        self.level_buttons: dict[str, tk.Button] = {}

        root.title("Chessboard Lab — Stockfish Simulator")
        root.configure(bg=CREAM)
        root.minsize(815, 660)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self._render()
        self.root.after(60, self._poll_responses)

    def _build(self) -> None:
        shell = tk.Frame(self.root, bg=CREAM, padx=30, pady=23)
        shell.pack(fill="both", expand=True)

        header = tk.Frame(shell, bg=CREAM)
        header.pack(fill="x", pady=(0, 20))
        tk.Label(header, text="♞  CHESSBOARD LAB", bg=CREAM, fg=INK,
                 font=("Helvetica", 15, "bold")).pack(side="left")
        tk.Label(header, text="COMPUTER MODE  ·  OFFLINE", bg=CREAM, fg="#66806d",
                 font=("Helvetica", 10, "bold")).pack(side="right")

        main = tk.Frame(shell, bg=CREAM)
        main.pack(fill="both", expand=True)

        left = tk.Frame(main, bg=PAPER, padx=17, pady=14, highlightbackground="#ded9cf", highlightthickness=1)
        left.pack(side="left", fill="both", expand=True, padx=(0, 22))
        tk.Label(left, text="♞  STOCKFISH", bg=PAPER, fg=INK,
                 font=("Helvetica", 11, "bold"), anchor="w").pack(fill="x", pady=(0, 9))

        board_edge = tk.Frame(left, bg="#d6b995", padx=7, pady=7)
        board_edge.pack(fill="both", expand=True)
        board_frame = tk.Frame(board_edge, bg=DARK)
        board_frame.pack(fill="both", expand=True)
        for row in range(8):
            board_frame.grid_rowconfigure(row, weight=1, uniform="square")
            board_frame.grid_columnconfigure(row, weight=1, uniform="square")
            for column in range(8):
                square = f"{'abcdefgh'[column]}{8 - row}"
                button = tk.Button(
                    board_frame, text="", relief="flat", borderwidth=0,
                    highlightthickness=0, padx=0, pady=0,
                    font=("Arial Unicode MS", 37),
                    command=lambda name=square: self._click_square(name),
                )
                button.grid(row=row, column=column, sticky="nsew")
                self.squares[square] = button
        tk.Label(left, text="♙  YOU  ·  WHITE", bg=PAPER, fg=INK,
                 font=("Helvetica", 11, "bold"), anchor="w").pack(fill="x", pady=(10, 0))

        right = tk.Frame(main, bg=PAPER, padx=23, pady=23, width=300,
                         highlightbackground="#ded9cf", highlightthickness=1)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        tk.Label(right, text="01 / GAME SETUP", bg=PAPER, fg=ACCENT,
                 font=("Helvetica", 10, "bold"), anchor="w").pack(fill="x")
        tk.Label(right, text="Choose your\nchallenge.", bg=PAPER, fg=INK,
                 justify="left", font=("Georgia", 28), anchor="w").pack(fill="x", pady=(13, 8))
        tk.Label(right, text="Choose a difficulty before starting a game.", bg=PAPER,
                 fg="#818b82", wraplength=244, justify="left",
                 font=("Helvetica", 11), anchor="w").pack(fill="x", pady=(0, 16))

        for key, difficulty in DIFFICULTIES.items():
            button = tk.Button(right, text=f"{difficulty.label}\n{difficulty.description}",
                               justify="left", anchor="w", padx=14, pady=10,
                               font=("Helvetica", 11), relief="flat", borderwidth=1,
                               command=lambda name=key: self._choose_level(name))
            button.pack(fill="x", pady=4)
            self.level_buttons[key] = button

        self.start_button = tk.Button(right, text="Start game  ↗", command=self._start_game,
                                      bg="#2c493a", fg=PAPER, activebackground="#395f49",
                                      activeforeground=PAPER, relief="flat", borderwidth=0,
                                      pady=11, font=("Helvetica", 11, "bold"))
        self.start_button.pack(fill="x", pady=(17, 7))
        tk.Label(right, textvariable=self.note, bg=PAPER, fg="#92998f",
                 wraplength=244, font=("Helvetica", 10)).pack(fill="x")

        tk.Frame(right, height=1, bg="#e7e5dc").pack(fill="x", pady=21)
        tk.Label(right, text="02 / GAME STATUS", bg=PAPER, fg=ACCENT,
                 font=("Helvetica", 10, "bold"), anchor="w").pack(fill="x")
        tk.Label(right, textvariable=self.status, bg="#edf2eb", fg="#385744",
                 font=("Helvetica", 11), wraplength=218, justify="left",
                 anchor="w", padx=12, pady=13).pack(fill="x", pady=(10, 22))

        tk.Label(right, text="MOVE HISTORY", bg=PAPER, fg=ACCENT,
                 font=("Helvetica", 10, "bold"), anchor="w").pack(fill="x")
        self.history = tk.Text(right, width=25, height=7, wrap="word", bg="#f7f7f2",
                               fg=INK, relief="flat", padx=10, pady=8,
                               font=("Courier", 11), state="disabled")
        self.history.pack(fill="both", expand=True, pady=(10, 0))

        tk.Label(shell, text="STOCKFISH RUNS LOCALLY  ·  NO INTERNET REQUIRED WHILE PLAYING",
                 bg=CREAM, fg="#9b9e94", font=("Helvetica", 9, "bold"),
                 anchor="w").pack(fill="x", pady=(17, 0))

    def _choose_level(self, level: str) -> None:
        self.level_choice.set(level)
        if self.game.started and level != self.game.level:
            self.note.set(f"{DIFFICULTIES[level].label} applies to the next game.")
        else:
            self.note.set("You play White and move first.")
        self._render_levels()

    def _start_game(self) -> None:
        self.game.start(self.level_choice.get())
        self.selected = None
        self.thinking = False
        self.note.set("You play White and move first.")
        self.status.set("Your move. Select a piece, then a highlighted square.")
        self._render()

    def _click_square(self, square: str) -> None:
        if not self.game.started or self.thinking or self.game.result_text():
            return
        if self.selected == square:
            self.selected = None
            self._render_board()
            return
        piece = self.game.board.piece_at(chess.parse_square(square))
        if piece and piece.color == chess.WHITE:
            self.selected = square
            self.status.set("Choose a highlighted square.")
            self._render_board()
            return
        if self.selected:
            if square in self.game.legal_destinations(self.selected):
                self._play_human(self.selected, square)
            else:
                self.status.set("That move is not legal. Choose a highlighted square.")

    def _play_human(self, source: str, target: str, promotion: str | None = None) -> None:
        try:
            self.game.play_human(source, target, promotion)
        except PromotionRequired:
            self._ask_promotion(source, target)
            return
        except ValueError as exc:
            self.status.set(str(exc))
            return

        self.selected = None
        self._render()
        result = self.game.result_text()
        if result:
            self.status.set(result)
        else:
            self._ask_stockfish()

    def _ask_promotion(self, source: str, target: str) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Pawn promotion")
        dialog.configure(bg=PAPER)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        tk.Label(dialog, text="Choose a promotion piece", bg=PAPER, fg=INK,
                 font=("Georgia", 18)).pack(padx=23, pady=(20, 10))
        choices = tk.Frame(dialog, bg=PAPER)
        choices.pack(padx=18, pady=(0, 20))
        for code, label, glyph in (("q", "Queen", "♕"), ("r", "Rook", "♖"),
                                   ("b", "Bishop", "♗"), ("n", "Knight", "♘")):
            def choose(piece: str = code) -> None:
                dialog.destroy()
                self._play_human(source, target, piece)

            tk.Button(choices, text=f"{glyph}\n{label}", command=choose,
                      bg="#edf2eb", fg=INK, relief="flat", padx=11, pady=8,
                      font=("Helvetica", 14)).pack(side="left", padx=3)
        dialog.grab_set()
        dialog.focus_set()

    def _ask_stockfish(self) -> None:
        self.thinking = True
        self.status.set(f"Stockfish is thinking · {DIFFICULTIES[self.game.level].label}")
        self._render_board()
        revision = self.game.revision
        board = self.game.board.copy(stack=True)
        future = self.executor.submit(self.player.choose_move, board, self.game.level)
        future.add_done_callback(lambda done: self.responses.put((revision, done)))

    def _poll_responses(self) -> None:
        while not self.responses.empty():
            revision, future = self.responses.get_nowait()
            if revision != self.game.revision or not self.thinking:
                continue
            self.thinking = False
            try:
                self.game.play_computer(future.result())
            except Exception as exc:
                self.status.set(f"Stockfish error: {exc}. Start a new game to retry.")
                self._render_board()
                continue
            self._render()
            self.status.set(self.game.result_text() or
                            ("Check — protect your king." if self.game.board.is_check()
                             else "Your move. Select a piece, then a highlighted square."))
        if not self.closing:
            self.root.after(60, self._poll_responses)

    def _render_levels(self) -> None:
        for name, button in self.level_buttons.items():
            selected = self.level_choice.get() == name
            button.configure(bg="#edf2eb" if selected else "#fffefb",
                             fg="#2c493a" if selected else INK,
                             activebackground="#edf2eb",
                             highlightbackground="#5d8068" if selected else "#e3e3d9")

    def _render_board(self) -> None:
        legal = self.game.legal_destinations(self.selected) if self.selected else set()
        playable = self.game.started and not self.thinking and not self.game.result_text()
        last = set()
        if self.game.last_move:
            last = {chess.square_name(self.game.last_move.from_square),
                    chess.square_name(self.game.last_move.to_square)}
        for square, button in self.squares.items():
            file = ord(square[0]) - ord("a")
            rank = int(square[1])
            light = (file + rank) % 2 == 0
            background = LIGHT if light else DARK
            if square in last:
                background = "#e9dfa9" if light else "#a0aa71"
            if square in legal:
                background = "#d4c47e"
            if square == self.selected:
                background = "#d2b46c"
            piece = self.game.board.piece_at(chess.parse_square(square))
            if piece and piece.piece_type == chess.KING and piece.color == self.game.board.turn and self.game.board.is_check():
                background = "#cc7667"
            button.configure(text=PIECES[piece.symbol()] if piece else ("·" if square in legal else ""),
                             bg=background, activebackground=background,
                             fg="#fffaf1" if piece and piece.color == chess.WHITE else INK,
                             activeforeground="#fffaf1" if piece and piece.color == chess.WHITE else INK,
                             state="normal" if playable else "disabled")

    def _render_history(self) -> None:
        moves = self.game.san_moves
        lines = [f"{index // 2 + 1:>2}.  {moves[index]:<8} {moves[index + 1] if index + 1 < len(moves) else ''}"
                 for index in range(0, len(moves), 2)]
        self.history.configure(state="normal")
        self.history.delete("1.0", "end")
        self.history.insert("1.0", "\n".join(lines) if lines else "Your moves will appear here.")
        self.history.configure(state="disabled")

    def _render(self) -> None:
        self._render_levels()
        self._render_board()
        self._render_history()
        self.start_button.configure(text="New game  ↗" if self.game.started else "Start game  ↗")

    def close(self) -> None:
        self.closing = True
        self.root.destroy()
        self.executor.submit(self.player.close)
        self.executor.shutdown(wait=False)


def main() -> int:
    try:
        player = StockfishPlayer()
    except (FileNotFoundError, RuntimeError, OSError) as exc:
        print(f"Cannot start simulator: {exc}", file=sys.stderr)
        return 1

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        player.close()
        print(f"Cannot open the desktop window: {exc}", file=sys.stderr)
        return 1
    ChessSimulator(root, player)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
