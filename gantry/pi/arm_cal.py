#!/usr/bin/env python3
"""Find the servo arm's up and down angles by hand, on the Pi, with the ESP32 on USB (firmware 0.8.2+).

    ~/chess-board-pi-only/.venv/bin/python ~/chess-gantry/gantry/pi/arm_cal.py [start_deg] [--zero RAW]

--zero RAW: count angles from the board's RAW degree (shown as 0) and mark it as UP; the config still gets the
board's own degrees. start_deg is then counted from it too (default 0).

Keys:  left/right or a/d  -1/+1 deg      down/up or A/D  -5/+5 deg
       f  let go (limp)   h  hold here   u  this is UP   b  this is DOWN (bottom)
       t  test: up, wait, down           s  save up/down to gantry_config.json     q  quit (limp)
Nothing travels: only the arm moves. The arm is driven through the firmware's down slot ($S=up,<deg> with
the arm held down moves it there at once), so it never sweeps from 0.
"""
import json, os, select, shutil, subprocess, sys, termios, time, tty

import serial

PORT = os.environ.get("CHESS_GANTRY_PORT_DEV", "/dev/ttyUSB0")
CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gantry_config.json")


def main() -> int:
    busy = subprocess.run(["fuser", PORT], capture_output=True, text=True).stdout.strip()
    if busy:
        print(f"{PORT} is open in another program (pid {busy}): close the console / gantry terminal first.")
        return 1
    cfg = json.load(open(CONFIG))
    saved = cfg.get("servo_deg") or {}
    up, down = saved.get("up"), saved.get("down")
    args = sys.argv[1:]
    zero = 0
    if "--zero" in args:
        i = args.index("--zero"); zero = int(args[i + 1]); del args[i:i + 2]
        up = zero
    if args:
        angle = zero + int(args[0])
    else:
        angle = zero if "--zero" in sys.argv else int(down if down is not None else 90)
    rel = lambda raw: "-" if raw is None else f"{raw - zero:+d}" + (f" (board {raw})" if zero else "")
    settle = int(cfg.get("servo_settle_ms", 3000))

    s = serial.Serial(PORT, 115200, timeout=0.2)   # opening resets the ESP32: it boots limp (0.8.2)
    time.sleep(2.5)
    banner = s.read(4000).decode(errors="replace")
    if "0.8.2" not in banner and "0.8.3" not in banner:
        s.write(b"$I\r\n"); time.sleep(0.5); banner += s.read(2000).decode(errors="replace")
    if any(f"duen-gantry 0.8.{n}" in banner for n in (0, 1)):
        print("This board runs older firmware that drives the arm on boot: flash 0.8.6 first."); return 1
    if any(f"duen-gantry 0.8.{n}" in banner for n in (2, 3, 4)):
        print("This board's degrees are not real degrees (0.8.4 and older): flash 0.8.6 first."); return 1
    if "duen-gantry 0.8.5" in banner:
        print("Note: 0.8.5 stops at 0..180; 0.8.6 goes -20..200.")

    def cmd(text: str) -> str:
        s.reset_input_buffer()
        s.write((text + "\r\n").encode())
        end = time.monotonic() + 1.5
        while time.monotonic() < end:
            line = s.readline().decode(errors="replace").strip()
            if line == "ok" or line.startswith("error"):
                return line
        return "no answer"

    cmd("$X")   # out of the not-homed alarm (nothing moves) so M7 is allowed for the test
    held = False

    def hold(deg: int) -> None:
        nonlocal held
        r = cmd(f"$S={up if up is not None else 90},{deg}")
        if r != "ok":
            say(f"$S refused: {r}"); return
        if not held:
            r = cmd("M8")
            held = r == "ok"
            if not held: say(f"M8 refused: {r}")

    msg = ""
    def say(text: str) -> None:
        nonlocal msg
        msg = text

    def draw() -> None:
        state = f"holding {rel(angle)}" if held else f"LIMP (next move holds at {rel(angle)})"
        sys.stdout.write(f"\r\033[K{state} | up={rel(up)} down={rel(down)} | {msg}")
        sys.stdout.flush()

    print(__doc__.split("Keys:")[1].split("Nothing")[0].replace("\n       ", "\n  "))
    print("Starts LIMP. Press h to take hold at the start angle, then nudge it.\n")
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    try:
        draw()
        while True:
            if not select.select([sys.stdin], [], [], 0.5)[0]:
                continue
            k = os.read(fd, 3).decode(errors="ignore")
            step = {"\x1b[D": -1, "a": -1, "\x1b[C": 1, "d": 1, "\x1b[B": -5, "A": -5, "\x1b[A": 5, "D": 5}.get(k)
            msg = ""
            if step is not None:
                angle = max(-20, min(200, angle + step)); hold(angle)
            elif k == "h":
                hold(angle)
            elif k == "f":
                say(cmd("M9")); held = False
            elif k == "u":
                up = angle; say(f"UP = {rel(up)}")
            elif k == "b":
                down = angle; say(f"DOWN = {rel(down)}")
            elif k == "t":
                if up is None or down is None:
                    say("mark both u and b first")
                else:
                    held = False
                    r1 = cmd(f"$S={up},{down}") if cmd("M8") == "ok" else "M8 refused"
                    r2 = cmd("M7"); draw(); time.sleep(max(1.0, settle / 1000))
                    r3 = cmd("M8"); time.sleep(max(1.0, settle / 1000))
                    held = r3 == "ok"; angle = down
                    say(f"test: $S {r1}, up {r2}, down {r3}")
            elif k == "s":
                if up is None or down is None:
                    say("mark both u and b first")
                else:
                    shutil.copy(CONFIG, CONFIG + time.strftime(".before-arm-cal-%Y%m%d-%H%M%S"))
                    cfg = json.load(open(CONFIG))
                    cfg["servo_deg"] = {"up": int(up), "down": int(down)}
                    json.dump(cfg, open(CONFIG, "w"), indent=2)
                    say(f"saved up={up} down={down} (old config backed up)")
            elif k in ("q", "\x03"):
                break
            draw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        cmd("M9")
        s.close()
        print(f"\nArm left limp. up={rel(up)} down={rel(down)} (config gets the board's degrees: up={up} down={down})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
