#!/usr/bin/env python3
"""Tour every square of the board with the gantry, starting with the carriage sitting on a1.

The carriage is assumed to be under the centre of a1 (bottom-left as the person sees it) when this
starts. That spot is declared home (0,0) with $HM, and every square is --square mm (50) from its
neighbour: b1 is 50 mm in +X, a2 is 50 mm in +Y, h8 is at (350, 350). The tour snakes through the
ranks (a1..h1, h2..a2, a3..h3, ...), rests on each square, prints what the board reports against what
was asked, and comes back to a1. Only the two steppers run unless --servo is given, which lifts and
lowers the arm on every square. Ctrl-C stops the board at once.

    tour.py                      all 64 squares at 60 mm/s
    tour.py --corners            just a1, h1, h8, a8, e4, a1
    tour.py --speed 100 --pause 0 --servo
    tour.py --no-home            already homed with a1 at 0,0: do not send $HM
    tour.py --a1 60,35           a1 is 60 mm right and 35 mm up from the home spot
"""
import argparse
import glob
import math
import sys
import time

import serial

FILES = "abcdefgh"


def serpentine():
    """a1..h1, then h2..a2, then a3..h3 ...: every square once, each move one square long."""
    for rank in range(8):
        files = range(8) if rank % 2 == 0 else range(7, -1, -1)
        for f in files:
            yield FILES[f] + str(rank + 1), f, rank


def corners():
    for name in ("a1", "h1", "h8", "a8", "e4", "a1"):
        yield name, FILES.index(name[0]), int(name[1]) - 1


class Board:
    def __init__(self, port: str):
        self.port = serial.Serial(port, 115200, timeout=0.05)
        time.sleep(0.5)
        self.port.reset_input_buffer()

    def send(self, line: str, timeout: float = 2.0) -> str:
        """Send one line; return the board's answer: ok, error:..., or a <status> line."""
        self.port.write((line + "\n").encode())
        self.port.flush()
        end = time.monotonic() + timeout
        pending = ""
        while time.monotonic() < end:
            chunk = self.port.read(self.port.in_waiting or 1).decode("utf-8", "replace")
            if not chunk:
                continue
            pending += chunk
            while "\n" in pending:
                text, pending = pending.split("\n", 1)
                text = text.strip()
                if text == "ok" or text.startswith("error:") or text.startswith("<"):
                    return text
        raise RuntimeError(f"no answer to {line!r} in {timeout:.0f} s (is the board on USB and powered?)")

    def status(self) -> str:
        return self.send("?")

    def wait_idle(self, timeout: float) -> str:
        end = time.monotonic() + timeout
        while True:
            s = self.status()
            if s.startswith("<Idle"):
                return s
            if s.startswith("<Alarm") or s.startswith("<Hold"):
                raise RuntimeError(f"the board stopped: {s}")
            if time.monotonic() > end:
                raise RuntimeError(f"the move did not finish in {timeout:.0f} s: {s}")
            time.sleep(0.05)

    @staticmethod
    def position(status: str) -> tuple[float, float]:
        for field in status.strip("<>").split("|"):
            if field.startswith("MPos:"):
                x, y = field[5:].split(",")
                return float(x), float(y)
        return math.nan, math.nan


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port (default: the first /dev/cu.usbserial-*)")
    ap.add_argument("--speed", type=float, default=60.0, help="mm/s between squares (default 60; the firmware caps it at its max)")
    ap.add_argument("--pause", type=float, default=0.3, help="seconds to rest on each square (default 0.3)")
    ap.add_argument("--square", type=float, default=50.0, help="mm from one square's centre to the next (default 50)")
    ap.add_argument("--a1", default="0,0", help="a1's centre from the home spot in mm (default 0,0: the carriage starts ON a1)")
    ap.add_argument("--servo", action="store_true", help="lift and lower the arm on every square")
    ap.add_argument("--corners", action="store_true", help="only a1, h1, h8, a8, e4, a1")
    ap.add_argument("--no-home", action="store_true", help="the board is already homed: do not declare the current spot as home")
    args = ap.parse_args()

    port = args.port or next(iter(sorted(glob.glob("/dev/cu.usbserial-*"))), None)
    if not port:
        print("No ESP32 on USB (no /dev/cu.usbserial-*). Plug it in and try again.")
        return 1
    a1x, a1y = (float(v) for v in args.a1.split(","))
    feed = int(round(args.speed * 60))
    route = list(corners() if args.corners else serpentine())

    board = Board(port)
    print(f"== {port}: {board.status()}")
    if args.no_home:
        if "|Homed:1" not in board.status():
            print("The board is not homed. Run without --no-home with the carriage on a1, or home.sh manual.")
            return 1
    else:
        answer = board.send("$HM")
        if answer != "ok":
            print(f"$HM refused: {answer}")
            return 1
        print("== this spot is now home: a1 is at", f"({a1x:g}, {a1y:g}) mm from it")
    board.send("M8")   # arm down before any travel
    print(f"== {len(route)} squares at {args.speed:g} mm/s, {args.square:g} mm apart; Ctrl-C stops the board\n")

    here = board.position(board.status())
    started = time.monotonic()
    wrong = 0
    try:
        for name, f, r in route:
            x, y = a1x + f * args.square, a1y + r * args.square
            distance = math.hypot(x - here[0], y - here[1])
            answer = board.send(f"G1 X{x:.3f} Y{y:.3f} F{feed}")
            if answer != "ok":
                raise RuntimeError(f"{name}: the board refused the move: {answer}")
            status = board.wait_idle(distance / max(args.speed, 1.0) + 5.0)
            px, py = board.position(status)
            ok = abs(px - x) < 0.01 and abs(py - y) < 0.01
            wrong += 0 if ok else 1
            print(f"{name:>3}  asked ({x:6.1f}, {y:6.1f})   board ({px:6.1f}, {py:6.1f}){'' if ok else '   <- not where it was told'}")
            here = (px, py)
            if args.servo:
                board.send("M7"); time.sleep(0.6)
                board.send("M8"); time.sleep(0.6)
            time.sleep(args.pause)
        if here != (a1x, a1y):
            answer = board.send(f"G1 X{a1x:.3f} Y{a1y:.3f} F{feed}")
            if answer != "ok":
                raise RuntimeError(f"back to a1: the board refused: {answer}")
            status = board.wait_idle(math.hypot(a1x - here[0], a1y - here[1]) / max(args.speed, 1.0) + 5.0)
            print(f"\n a1  back home: board {board.position(status)}")
    except KeyboardInterrupt:
        board.send("!")
        print("\nStopped. The board is holding: run `bash home.sh manual` with the carriage back on a1 before the next move.")
        return 130
    except RuntimeError as problem:
        print(f"\n{problem}")
        try:
            board.send("!")
        except Exception:
            pass
        return 1
    took = time.monotonic() - started
    print(f"\n== {len(route)} squares in {took:.0f} s; the board's count matched on every square"
          if not wrong else f"\n== {len(route)} squares in {took:.0f} s; {wrong} did NOT match the asked position")
    print("   Watch the carriage, not the numbers: the board counts steps and cannot see a motor that stalls or a belt that slips.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
