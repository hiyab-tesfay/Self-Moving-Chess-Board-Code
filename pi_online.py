"""Online mode for the touch screen: the website plays on this board.

The Pi's game server (duen-relay, from kennynakao/chess-website) accepts website games only
while the screen has Online selected: the screen sends it a heartbeat every second, and
stops when the person leaves Online. The first heartbeat after the camera verified the
starting position carries `fresh`, so the server starts from an empty board instead of a
game somebody left behind. Until a connected browser starts a game the screen shows the
website address and the board's access code; then it shows the game as the website plays
it; when the game ends the pieces go home ("Putting the pieces back…") and the address and
code come back with the result. End Game / Reset Board on the screen send `reset` once.
Nothing here touches the camera or the in-person game.

Settings come from ~/.config/duen-chess/relay.env, the same file the game server reads:
  CHESS_RELAY_TOKEN   required; the game server's token
  CHESS_RELAY_URL     where the game server listens (default http://127.0.0.1:8765)
  CHESS_WEBSITE_URL   the address to show (default https://chess-website-gules.vercel.app)
  CHESS_ACCESS_CODE   the code to show; the website asks for it when someone connects
"""

from __future__ import annotations

import json
import os
import queue
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import chess

from move_observation import occupied_squares

RELAY_ENV = Path(os.environ.get("CHESS_RELAY_ENV") or (Path.home() / ".config" / "duen-chess" / "relay.env"))
MAX_UNCLEAR_SQUARES = 2
DEFAULT_RELAY_URL = "http://127.0.0.1:8765"
DEFAULT_WEBSITE_URL = "https://chess-website-gules.vercel.app"
HEARTBEAT_SECONDS = 1.0
RETRY_SECONDS = 2.0


class OnlineError(Exception):
    """Something the screen should show in plain words."""


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines, with optional `export`, quotes and comments."""
    values: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


@dataclass(frozen=True)
class OnlineSettings:
    token: str
    relay_url: str
    website_url: str
    code: str | None

    @classmethod
    def load(cls, path: Path = RELAY_ENV) -> "OnlineSettings":
        env = read_env_file(path)
        token = env.get("CHESS_RELAY_TOKEN", "")
        if len(token) < 32:
            raise OnlineError("The game server is not set up on this Pi (no CHESS_RELAY_TOKEN in relay.env).")
        return cls(token, env.get("CHESS_RELAY_URL") or DEFAULT_RELAY_URL,
                   env.get("CHESS_WEBSITE_URL") or DEFAULT_WEBSITE_URL,
                   env.get("CHESS_ACCESS_CODE") or env.get("CHESS_DEVICE_CODE") or None)

    @property
    def website_display(self) -> str:
        return self.website_url.replace("https://", "").replace("http://", "").rstrip("/")


class RelayClient:
    """Talks to the game server on this Pi as the screen's own session."""

    def __init__(self, settings: OnlineSettings, session: str | None = None, timeout: float = 2.0) -> None:
        self.settings = settings
        self.session = session or "touchscreen-" + uuid4().hex
        self.timeout = timeout

    def heartbeat(self, active: bool = True, fresh: bool = False, reset: bool = False) -> dict:
        """Tell the server whether Online is selected, and get the state the screen should show.

        `fresh` goes with the first heartbeat after the camera verified the starting position: the
        server drops the old game and its lease and starts from an empty, not-started board game.
        `reset` is the screen's End Game / Reset Board: the server ends any running game as
        abandoned, queues the camera-verified reset job and clears the lease.
        """
        message: dict = {"active": active}
        if fresh:
            message["fresh"] = True
        if reset:
            message["reset"] = True
        body = json.dumps(message).encode()
        request = urllib.request.Request(
            self.settings.relay_url.rstrip("/") + "/api/online", data=body, method="POST",
            headers={"Authorization": "Bearer " + self.settings.token, "X-Player-Session": self.session,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as reply:
                data = json.loads(reply.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise OnlineError("The game server rejected the token in relay.env.") from None
            raise OnlineError(f"The game server answered HTTP {exc.code}.") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise OnlineError("The game server is not running on this Pi (duen-relay).") from exc
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise OnlineError("The game server sent an unexpected answer.")
        return data


def starting_position_problem(occupied, uncertain=()) -> str | None:
    """What the camera sees wrong with the starting position, in plain words; None when it is all there.

    `occupied` is the set of squares the vision code saw a piece on, `uncertain` the squares it
    could not tell. The website may only take the board once this returns None.
    """
    occupied = frozenset(occupied)
    unclear = set(uncertain)
    if len(unclear) > MAX_UNCLEAR_SQUARES:
        return "The camera cannot tell about " + ", ".join(sorted(unclear)) + ". Check the lighting, then tap Check Again."
    expected = occupied_squares(chess.Board())
    missing = sorted((expected - occupied) - unclear)
    extra = sorted((occupied - expected) - unclear)
    if not missing and not extra:
        return None
    parts = []
    if missing:
        parts.append("missing on " + ", ".join(missing))
    if extra:
        parts.append("extra on " + ", ".join(extra))
    return "Set up the starting position: " + "; ".join(parts) + ". Then tap Check Again."


@dataclass(frozen=True)
class OnlineView:
    connected: bool
    board: chess.Board
    last_move: chess.Move | None
    title: str
    detail: str
    status: str
    # What the screen draws and offers. "pairing": the address and code (Reset Board); "game": the position
    # as the website plays it (End Game); "resetting": the final position while the pieces go home (no
    # button); "fix_setup": the reset stopped, the person sets up the start position and taps Check Again.
    mode: str = "game"


ENDED_TITLES = {"timeout": "Out of time", "resignation": "Game over", "abandoned": "Game over"}


def describe_online(snapshot: dict, settings: OnlineSettings) -> OnlineView:
    """What the screen shows for a game-server state: the pairing screen, or the live game.

    A player is connected only while a browser talks to the Pi (`clients`); a stale lease on an old
    game (`occupied`) no longer counts, so Online never opens on a game nobody is playing.
    """
    connected = int(snapshot.get("clients") or 0) > 0
    board = chess.Board()
    fen = snapshot.get("fen")
    if isinstance(fen, str):
        try:
            board = chess.Board(fen)
        except ValueError:
            board = chess.Board()
    last_move = None
    if isinstance(snapshot.get("lastMove"), str):
        try:
            last_move = chess.Move.from_uci(snapshot["lastMove"])
        except ValueError:
            last_move = None
    where = f"Open {settings.website_display}" + (" and enter the code." if settings.code else ".")
    motion = snapshot.get("motion") or {}
    state, stage, current = motion.get("state"), motion.get("stage"), motion.get("current") or ""
    game_over = bool(snapshot.get("gameOver"))
    started = bool(snapshot.get("started"))

    def moving() -> OnlineView:
        if stage == "stockfish":
            return OnlineView(connected, board, last_move, "Stockfish thinking", "Leave the board as it is.", "Stockfish is thinking…")
        if stage == "camera":
            return OnlineView(connected, board, last_move, f"Camera checking {current}", "Checking the pieces on the board.", f"Camera checking {current}…")
        return OnlineView(connected, board, last_move, f"Gantry moving {current}", "Keep hands clear of the board.", f"Gantry moving {current}…")

    # The pieces are going home: after a game ended, or after Reset Board on the screen.
    if state == "moving" and current == "reset":
        status = "Checking the pieces…" if stage == "camera" else "Putting the pieces back…"
        if game_over:
            reason = (snapshot.get("ended") or {}).get("reason") or (snapshot.get("outcome") or {}).get("reason")
            title = ENDED_TITLES.get(reason, "Game over")
            return OnlineView(connected, board, last_move, title, snapshot.get("result") or "The game has ended.", status, mode="resetting")
        return OnlineView(connected, board, last_move, "Resetting the board", "Every piece goes back to its starting square.", status, mode="resetting")
    # The reset stopped (a game is not on): the start position must be set up by hand and checked by the camera.
    if state == "fault" and (game_over or not started):
        error = (motion.get("error") or "").strip()
        return OnlineView(connected, board, last_move, "Board stopped",
                          (error + " " if error else "") + "Set up the starting position by hand, then tap Check Again.",
                          "The board stopped. Set up the starting position, then tap Check Again.", mode="fix_setup")
    if game_over:
        if state == "moving":
            return moving()   # the last move is still being carried; the reset follows
        result = snapshot.get("result") or "The game has ended."
        return OnlineView(connected, board, last_move, "Online", f"Last game: {result} {where}",
                          f"Last game: {result} Waiting for a player to connect…", mode="pairing")
    if not connected:
        return OnlineView(False, board, last_move, "Online", where, "Waiting for a player to connect…", mode="pairing")
    if not started:
        return OnlineView(True, board, last_move, "Player connected", "Waiting for them to start a game on the website.",
                          "A player is connected. Waiting for them to start a game on the website.", mode="pairing")
    kind = "the pieces move on this board" if snapshot.get("board") else "digital game, the pieces stay where they are"
    if state == "fault":
        return OnlineView(True, board, last_move, "Board stopped",
                          (motion.get("error") or "") + " Set the pieces to match the screen, then confirm on the website.",
                          "The board stopped. Fix the pieces, then confirm on the website.")
    if state == "waiting":
        steps = " ".join(motion.get("instructions") or [])
        return OnlineView(True, board, last_move, "Move the piece by hand", steps or "Follow the website.", steps or "Waiting for the move on the board.")
    if state == "moving":
        return moving()
    if snapshot.get("turn") == "b":
        return OnlineView(True, board, last_move, "Stockfish to move", f"Online game: {kind}.", "Stockfish to move.")
    return OnlineView(True, board, last_move, "Player to move", f"Waiting for the move from the website ({kind}).", "Waiting for the player's move.")


class OnlineSession:
    """Online mode while it is selected: a thread that heartbeats and hands each state to the screen.

    Results arrive on the screen's queue as (generation, "online", snapshot, error), the same
    shape as the camera and engine work, so the Tk side handles them in one place.
    """

    def __init__(self, settings: OnlineSettings, results: queue.SimpleQueue, generation: int,
                 client: RelayClient | None = None) -> None:
        self.settings = settings
        self.client = client or RelayClient(settings)
        self.results = results
        self.generation = generation
        self.view: OnlineView | None = None
        self.error: str | None = None
        # The camera must see the full starting position before the website is let in.
        self.checking = False
        self.verified = False
        self.problem: str | None = None
        # One-shot flags for the next heartbeat ("fresh", "reset"); each stays until a heartbeat carrying it succeeded.
        self._pending: set[str] = set()
        self._flags = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.thread = threading.Thread(target=self._run, name="online-mode", daemon=True)

    def start(self) -> None:
        """Begin the heartbeat: from here on the game server accepts website games.

        The first heartbeat carries `fresh`, so the server drops whatever game another session left
        behind and starts from an empty board. Called again after a re-check, it sends `fresh` again.
        """
        self.verified = True
        self.problem = None
        self._request("fresh")
        if not self.thread.is_alive():
            self.thread.start()

    def request_reset(self) -> None:
        """End Game / Reset Board on the screen: the next heartbeat carries `reset`, once."""
        self._request("reset")

    @property
    def reset_pending(self) -> bool:
        with self._flags:
            return "reset" in self._pending

    def _request(self, flag: str) -> None:
        with self._flags:
            self._pending.add(flag)
        self._wake.set()

    def stop(self) -> None:
        """Leave Online: the thread's last heartbeat tells the game server so."""
        self._stop.set()
        self._wake.set()
        if not self.thread.is_alive() and not self.verified:
            return   # never started: the server was never told Online was on

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._flags:
                flags = frozenset(self._pending)
            try:
                snapshot = self.client.heartbeat(True, fresh="fresh" in flags, reset="reset" in flags)
                with self._flags:
                    self._pending -= flags
                self.results.put((self.generation, "online", snapshot, None))
                pause = HEARTBEAT_SECONDS
            except OnlineError as exc:
                self.results.put((self.generation, "online", None, exc))
                pause = RETRY_SECONDS
            self._wake.wait(pause)
            self._wake.clear()
        try:
            self.client.heartbeat(False)
        except OnlineError:
            pass
