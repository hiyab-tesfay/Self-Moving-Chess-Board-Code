#!/usr/bin/env python3
"""DUEN gantry terminal: fine tuning and movement from a text terminal, for the Raspberry Pi.

Runs on the Pi, which talks to the ESP32 over USB (USB-A on the Pi to USB-C on the ESP32, /dev/ttyUSB0).
From the Mac, open it over SSH:   bash pi_terminal.sh        (ssh -t smart@chessboard.local ...)
On the Pi itself:                 python gantry_term.py [--port /dev/ttyUSB0 | sim]

It drives the board with the same code as the browser console (console_server.Console): the same moves,
arm handling, Tune answers (saved to pi/gantry_config.json on the Pi) and safety checks. Type `help`.
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
import select
import shlex
import sys
import termios
import time
import tty
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import console_server as cs  # noqa: E402
import chess  # noqa: E402

gd = cs.gd

class Quiet(logging.Filter):
    """Hides the serial traffic (send / board lines) unless `verbose on`."""
    verbose = False

    def filter(self, record: logging.LogRecord) -> bool:
        if self.verbose:
            return True
        text = record.getMessage()
        return not (text.startswith("send ") or text.startswith("board ") or text.startswith("step "))


QUIET = Quiet()
STEPS = [0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 25.0, 50.0]

HELP = """\
Moving (mm; + is away from home: X toward the h-file, Y toward rank 8)
  x+ [mm]  x- [mm]  y+ [mm]  y- [mm]   jog (default 10 mm)        go <e4 | w:3 | 120,200>  go there
  keys                                  arrow keys / WASD jog, +/- change the step, u/j arm, Enter = right place
  turn x+|x-|y+|y-                      exactly one motor turn (to measure mm per turn)
Setting up
  home        this spot is 0,0 (push the carriage into the bottom-left corner first)
  unlock      clear an alarm ($X)            off     motors off (push by hand)        stop   stop everything
  up / down   the servo arm                  status  where it is
Fine tuning (answers are saved on the Pi in pi/gantry_config.json)
  tune <sq>   send the magnet to a square    then jog / keys to its real centre, then:
  ok          the magnet is on the centre (keeps the answer)
  tunes       list the answers               forget <sq> / forget all
Playing Stockfish (the gantry moves Stockfish's pieces; you move yours by hand and type the move)
  play [white|black|watch] [beginner|intermediate|advanced]   start or continue a game (watch: Stockfish v Stockfish)
  move <e2e4|Nf3>  the gantry plays one move      board  the game so far      new  pieces back at the start
  retry / before / after   a move that stopped part-way: try again, pieces were put back, or you finished it
Other
  speed travel|carry <mm/s>   armwait <s>    raw <line>  send any command to the ESP32 ($$, ?, M7, G1 X10 ...)
  verbose on|off  show the serial traffic          connect [port|sim]  disconnect   ports      quit"""


def find_port() -> str:
    for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*", "/dev/cu.usbserial*", "/dev/cu.SLAB*", "/dev/cu.usbmodem*"):
        found = sorted(glob.glob(pattern))
        if found:
            return found[0]
    return ""


def parse_move(board: chess.Board, text: str) -> chess.Move:
    """e2e4, e7e8q or SAN (Nf3, exd5, O-O). ValueError when it is not legal here."""
    text = text.strip()
    try:
        move = chess.Move.from_uci(text.lower())
        if move not in board.legal_moves and len(text) == 4:
            queen = chess.Move.from_uci(text.lower() + "q")
            if queen in board.legal_moves:
                move = queen
        if move in board.legal_moves:
            return move
    except ValueError:
        pass
    try:
        return board.parse_san(text)
    except ValueError:
        raise ValueError(f"{text} is not a legal move here ({'White' if board.turn else 'Black'} to move)") from None


def fit_text(fit: dict) -> str:
    """NudgeField.summary() in a line."""
    n = fit.get("count", 0)
    if not n:
        return "no answers yet"
    sx, sy = fit.get("shift", [0, 0])
    return (f"{n} square(s) tuned; the board fit is shifted {sx:+.2f}, {sy:+.2f} mm, "
            f"worst square off the fit by {fit.get('worst_leftover', 0):.2f} mm")


class Terminal:
    def __init__(self, console: cs.Console):
        self.c = console
        self.step = 10.0

    # ------------------------------------------------------------------ helpers

    def wait(self, quiet: bool = False) -> None:
        """Until the console's background task (a move, a connect) is done. Ctrl-C stops the gantry.
        When the task needs a person ("take the pawn off d5 by hand"), it asks here and waits for Enter."""
        try:
            while self.c.task:
                waiting = self.c.waiting
                if waiting:
                    input(f"  >> {waiting['prompt']}  [Enter when done] ")
                    try:
                        self.c.done()
                    except cs.Busy:
                        pass
                time.sleep(0.05)
        except (KeyboardInterrupt, EOFError):
            print("\nStopping.")
            self.c.stop()
            while self.c.task:
                time.sleep(0.05)
        if not quiet:
            self.show_status()

    def show_status(self) -> None:
        if self.c.link is None:
            print("  (not connected: `connect`)")
            return
        try:
            st = self.c.query_status()
        except Exception as exc:  # noqa: BLE001
            print(f"  status: {exc}")
            return
        tuning = (self.c.tuning or {}).get("square")
        print(f"  {st.get('state')}{' (' + st['reason'] + ')' if st.get('reason') else ''}  "
              f"X {st.get('x', 0):.2f}  Y {st.get('y', 0):.2f}  arm {st.get('servo') or '-'}  "
              f"{'homed' if st.get('homed') else 'NOT homed'}" + (f"  tuning {tuning}" if tuning else ""))

    @staticmethod
    def mm(args: list[str], default: float) -> float:
        return float(args[0]) if args else default

    # ------------------------------------------------------------------ commands

    def run(self, line: str) -> bool:
        """One command line. False = quit."""
        try:
            words = shlex.split(line)
        except ValueError as exc:
            print(f"  {exc}")
            return True
        if not words:
            return True
        cmd, args = words[0].lower(), words[1:]
        c = self.c
        if cmd in ("quit", "exit", "q"):
            return False
        if cmd in ("help", "h", "?") and not args and cmd != "?":
            print(HELP)
        elif cmd in ("x+", "x-", "y+", "y-"):
            d = self.mm(args, self.step) * (1 if cmd[1] == "+" else -1)
            c.jog(d if cmd[0] == "x" else 0.0, d if cmd[0] == "y" else 0.0)
            self.wait()
        elif cmd == "go":
            c.goto(" ".join(args))
            self.wait()
        elif cmd == "keys":
            self.keys()
        elif cmd == "turn":
            if not args or args[0] not in ("x+", "x-", "y+", "y-"):
                print("  turn x+ | x- | y+ | y-")
            else:
                c.one_turn(args[0][0], 1 if args[0][1] == "+" else -1)
                self.wait()
        elif cmd == "home":
            c.home_manual()
            self.show_status()
        elif cmd == "unlock":
            c.unlock()
            self.show_status()
        elif cmd == "off":
            c.motors_off()
        elif cmd == "stop":
            c.stop()
        elif cmd in ("up", "down"):
            c.servo(cmd == "up")
            print(f"  arm going {cmd} (about {c.settings['servo_settle']:g} s)")
        elif cmd in ("status", "s", "?"):
            self.show_status()
        elif cmd == "tune":
            if not args:
                print("  tune <square>, e.g. tune e4")
            else:
                c.tune_goto(args[0])
                self.wait()
                print(f"  Jog to {args[0]}'s real centre (x+/y- 0.5 or `keys`), then `ok`.")
        elif cmd == "ok":
            print("  saved. " + fit_text(c.tune_mark(args[0] if args else None)))
            self.next_hint()
        elif cmd == "tunes":
            answers = c._answers()
            if not answers:
                print("  no squares tuned yet")
            for sq, (dx, dy) in sorted(answers.items()):
                print(f"  {sq}: {dx:+.2f}, {dy:+.2f} mm")
        elif cmd == "forget":
            if args and args[0] == "all":
                c.tune_clear()
                print("  every answer cleared")
            elif args:
                c.tune_forget(args[0])
                print(f"  forgot {args[0]}")
            else:
                print("  forget <square> | forget all")
        elif cmd == "speed":
            if len(args) != 2 or args[0] not in ("travel", "carry"):
                print(f"  speed travel|carry <mm/s>   now travel {c.settings['travel_speed']:g}, "
                      f"carry {c.settings['carry_speed']:g}")
            else:
                c.save_settings({f"{args[0]}_speed": float(args[1])})
                print("  saved")
        elif cmd == "armwait":
            if not args:
                print(f"  armwait <s>   now {c.settings['servo_settle']:g} s")
            else:
                c.save_settings({"servo_settle": float(args[0])})
                print("  saved")
        elif cmd == "play":
            self.play(args)
        elif cmd == "move":
            if not args:
                print("  move <e2e4 | Nf3>")
            else:
                self.play_move(parse_move(c.game, args[0]), by_hand=False)
        elif cmd == "board":
            self.show_game()
        elif cmd == "new":
            c.new_game()
            print("  new game: the pieces should be in the starting position")
        elif cmd in ("retry", "before", "after"):
            c.resolve(cmd)
            self.wait()
        elif cmd == "verbose":
            QUIET.verbose = bool(args) and args[0] == "on"
            print(f"  serial traffic {'shown' if QUIET.verbose else 'hidden'}")
        elif cmd == "raw":
            for out in c.raw(" ".join(args)):
                print(f"  {out}")
        elif cmd == "connect":
            port = args[0] if args else find_port()
            if not port:
                print("  no ESP32 found on USB (/dev/ttyUSB*). Plug it in, or `connect sim` for the pretend board.")
            else:
                c.connect(port)
                self.wait()
        elif cmd == "disconnect":
            c.disconnect()
        elif cmd == "ports":
            ports = [p for pat in ("/dev/ttyUSB*", "/dev/ttyACM*", "/dev/cu.usb*") for p in sorted(glob.glob(pat))]
            print("  " + (", ".join(ports) if ports else "none"))
        else:
            print(f"  unknown: {cmd} (help)")
        return True

    # ------------------------------------------------------------------ chess

    def show_game(self) -> None:
        game = self.c.game
        print("\n".join("    " + row for row in str(game).splitlines()))
        moves = chess.Board().variation_san(game.move_stack) if game.move_stack else "(no moves yet)"
        print(f"  {moves}")
        if game.is_game_over():
            print(f"  game over: {game.result()}")
        else:
            print(f"  {'White' if game.turn else 'Black'} to move")

    def play_move(self, move: chess.Move, by_hand: bool) -> bool:
        """One move of the console's game. by_hand: the person moved the pieces; else the gantry carries them.
        True when the game has the move afterwards."""
        c = self.c
        before = len(c.game.move_stack)
        promo = chess.piece_symbol(move.promotion) if move.promotion else None
        preview = c.plan_move(chess.square_name(move.from_square), chess.square_name(move.to_square), promo, "chess")
        if not by_hand:
            print(f"  {preview.get('san', move.uci())}: {preview['carries']} carr{'y' if preview['carries'] == 1 else 'ies'}, "
                  f"about {preview['seconds']:.0f} s")
            if preview.get("warning"):
                print(f"  note: {preview['warning']}")
        try:
            c.run_preview(by_hand=by_hand)
        except gd.GantryError as exc:
            print(f"  the gantry cannot move it: {exc}")
            try:
                input(f"  >> Make {preview.get('san', move.uci())} by hand instead  [Enter when done, Ctrl-C to leave] ")
            except (KeyboardInterrupt, EOFError):
                c.cancel_preview()
                print()
                return False
            c.run_preview(by_hand=True)
        self.wait(quiet=True)
        if c.pending:
            print("  the move stopped part-way. Fix the board, then: retry | before | after")
            return False
        return len(c.game.move_stack) > before

    def play(self, args: list[str]) -> None:
        import stockfish_player as sp
        side = next((a for a in args if a in ("white", "black", "watch")), "white")
        level = next((a for a in args if a in sp.LEVELS), "intermediate")
        c = self.c
        if c.pending:
            print("  a move stopped part-way: retry | before | after first")
            return
        if c.game.is_game_over() or not c.game.move_stack:
            c.new_game()
            print("  new game: put the pieces in the starting position.")
        else:
            print("  continuing the game in progress (`new` first for a fresh one).")
        try:
            engine = sp.Engine(level)
        except (FileNotFoundError, ValueError, OSError) as exc:
            print(f"  {exc}")
            return
        engine_side = {"white": chess.BLACK, "black": chess.WHITE}.get(side)  # None: watch, Stockfish plays both
        who = "both sides" if engine_side is None else ("Black" if engine_side == chess.BLACK else "White")
        print(f"  Stockfish ({level}) plays {who}; the gantry moves its pieces. "
              "On your turn type your move after making it by hand (e2e4 or Nf3); "
              "`gantry <move>` has the gantry move your piece; `board`; `stop` leaves (the game is kept).")
        try:
            while not c.game.is_game_over():
                game = c.game
                if engine_side is None or game.turn == engine_side:
                    move = engine.choose(game)
                    print(f"  Stockfish: {game.san(move)}")
                    if not self.play_move(move, by_hand=False):
                        return
                    continue
                try:
                    text = input("your move> ").strip()
                except EOFError:
                    return
                if not text:
                    continue
                if text in ("stop", "quit", "q"):
                    return
                if text == "board":
                    self.show_game()
                    continue
                words = text.split()
                gantry = words[0] == "gantry" and len(words) == 2
                try:
                    move = parse_move(game, words[-1] if gantry else text)
                except ValueError as exc:
                    print(f"  {exc}")
                    continue
                if not self.play_move(move, by_hand=not gantry):
                    return
            self.show_game()
        except KeyboardInterrupt:
            print("\n  left the game (it is kept: `play` continues it)")
        finally:
            engine.close()

    def next_hint(self) -> None:
        done = set(self.c._answers())
        for sq in ("a1", "h1", "a8", "h8", "d4", "e5"):
            if sq not in done:
                print(f"  next: tune {sq}")
                return

    # ------------------------------------------------------------------ keys mode

    def keys(self) -> None:
        if not sys.stdin.isatty():
            print("  keys needs a real terminal (ssh -t)")
            return
        print(f"  keys: arrows/WASD jog {self.step:g} mm, +/- step, u arm up, j arm down, Enter right place, "
              f"space status, q back")
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while True:
                key = os.read(fd, 1)
                if key == b"\x1b":                       # an arrow key: ESC [ A..D
                    if select.select([fd], [], [], 0.05)[0]:
                        key = os.read(fd, 2)[-1:]
                        key = {b"A": b"w", b"B": b"s", b"C": b"d", b"D": b"a"}.get(key, b"")
                    else:
                        break
                k = key.decode(errors="ignore").lower()
                try:
                    if k in ("q",):
                        break
                    if k in "wasd" and k:
                        dx = {"a": -1, "d": 1}.get(k, 0) * self.step
                        dy = {"s": -1, "w": 1}.get(k, 0) * self.step
                        self.c.jog(dx, dy)
                    elif k in ("+", "="):
                        self.step = next((s for s in STEPS if s > self.step), self.step)
                        print(f"  step {self.step:g} mm")
                    elif k in ("-", "_"):
                        self.step = next((s for s in reversed(STEPS) if s < self.step), self.step)
                        print(f"  step {self.step:g} mm")
                    elif k == "u":
                        self.c.servo(True)
                    elif k == "j":
                        self.c.servo(False)
                    elif k in ("\n", "\r"):
                        self.wait(quiet=True)
                        print("  saved. " + fit_text(self.c.tune_mark()))
                        self.next_hint()
                    elif k == " ":
                        self.wait(quiet=True)
                        self.show_status()
                except (gd.GantryError, cs.Busy) as exc:
                    print(f"  {exc}")
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        self.wait()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", help="the ESP32's serial port (default: the first /dev/ttyUSB*), or sim")
    parser.add_argument("--config", type=Path, default=gd.DEFAULT_CONFIG)
    parser.add_argument("--sim-speed", type=float, default=1.0)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="  %(message)s")
    logging.getLogger("link").setLevel(logging.WARNING)
    for handler in logging.getLogger().handlers:
        handler.addFilter(QUIET)
    console = cs.Console(gd.load_config(args.config), args.sim_speed, args.config)
    term = Terminal(console)
    print("DUEN gantry terminal. `help` for commands, `quit` to leave.")
    port = args.port or find_port()
    if port:
        try:
            console.connect(port)
            term.wait()
        except Exception as exc:  # noqa: BLE001
            print(f"  {exc}")
    else:
        print("  no ESP32 on USB yet (/dev/ttyUSB*). Plug it in and type `connect`, or `connect sim`.")
    while True:
        try:
            line = input("gantry> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        try:
            if not term.run(line):
                break
        except (gd.GantryError, cs.Busy) as exc:
            print(f"  {exc}")
        except ValueError as exc:
            print(f"  {exc}")
    if console.link is not None:
        try:
            console.exchange("M5", 1.0)
            console.exchange("M8", 1.0)
        except Exception:  # noqa: BLE001
            pass
        console.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
