"""Validation for explicit laptop gantry commands, independent of the game engine."""
import math
import re


def valid_manual(value) -> bool:
    if not isinstance(value, dict):
        return False
    op = value.get("op")
    if op in ("connect", "disconnect", "status", "unlock", "forget", "home", "bump_home",
              "motors_off", "return_home", "start", "save_start", "arm_up", "arm_down", "magnet_on", "magnet_off"):
        return set(value) == {"op"}
    if op == "jog":
        return (set(value) == {"op", "dx", "dy", "step"} and
                all(type(value.get(key)) is int and value[key] in (-1, 0, 1) for key in ("dx", "dy")) and
                (value["dx"] != 0 or value["dy"] != 0) and
                type(value.get("step")) in (int, str) and value["step"] in (1, 5, 10, 50, "square"))
    if op == "goto":
        return set(value) == {"op", "place"} and isinstance(value.get("place"), str) and bool(
            re.fullmatch(r"(?:[a-h][1-8]|[bw]:[0-9]+|[0-9]+(?:\.[0-9]+)?,[0-9]+(?:\.[0-9]+)?)", value["place"]))
    if op in ("trace", "carry"):
        return set(value) == {"op", "uci"} and isinstance(value.get("uci"), str) and bool(re.fullmatch(r"[a-h][1-8][a-h][1-8]", value["uci"]))
    if op == "raw":
        line = value.get("line")
        return (set(value) == {"op", "line"} and isinstance(line, str) and 0 < len(line) <= 160 and
                line.isascii() and all(32 <= ord(char) < 127 for char in line) and
                line.startswith(("?", "!", "~", "$", "G", "M")))
    if op == "settings":
        limits = {"travel_speed_mm_s": (2, 120), "carry_speed_mm_s": (2, 120), "accel_mm_s2": (50, 800)}
        return (set(value) == {"op", *limits} and all(type(value[key]) in (int, float) and
                math.isfinite(value[key]) and lo <= value[key] <= hi for key, (lo, hi) in limits.items()))
    return False
