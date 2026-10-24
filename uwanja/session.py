"""One gauntlet's run: follows fastchess' log and PGN, rebuilds the games, reports them.

Everything polls on one thread (the files are the interface, fastchess needs nothing from us), the sender in
nyayo.py has the only other threads, one for the match document and one for the games. The run directory keeps what
was reported so it can be sent again later (`uwanja report`):

    gauntlet.toml     the file the run was started from
    identity.json     what was fixed at the start: engines, settings, host
    match.json        the latest match document
    games/<n>.pgn     every game, the latest PGN of it
    games.pgn         fastchess' own PGN, the games as it wrote them
    fastchess.log     fastchess' log, every UCI line
    fastchess.out     what fastchess printed
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Gauntlet
from .document import Identity, now_iso
from .events import parse_fastchess_line
from .nyayo import Reporter
from .pgnout import live_moves, live_pgn, read_games, to_finished
from .results import Results
from .schedule import FINISHED, STARTED, Entry, Schedule
from .tracker import FinishedGame, Game, Tracker

POLL_SECONDS = 0.1
DOCUMENT_SECONDS = 2.0       # at most this often when games finish, a match document is small but not free
HEARTBEAT_SECONDS = 30.0


@dataclass
class RunPaths:
    root: Path

    @property
    def config(self) -> Path: return self.root / "gauntlet.toml"
    @property
    def identity(self) -> Path: return self.root / "identity.json"
    @property
    def match(self) -> Path: return self.root / "match.json"
    @property
    def games(self) -> Path: return self.root / "games"
    @property
    def pgn(self) -> Path: return self.root / "games.pgn"
    @property
    def log(self) -> Path: return self.root / "fastchess.log"
    @property
    def out(self) -> Path: return self.root / "fastchess.out"
    @property
    def fastchess_config(self) -> Path: return self.root / "fastchess-config.json"


@contextmanager
def stop_on_signals():
    """SIGTERM (systemd, `kill`), SIGHUP (the terminal closed) and Ctrl-Break are a stop, as Ctrl-C is: fastchess is
    stopped and nyayo is told `stopped`, not left showing `running`. Only the main thread can take signals."""
    previous = {}

    def stop(number, frame):
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            previous[number] = signal.signal(number, stop)
        except (ValueError, OSError):
            pass
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def write_atomic(path: Path, text: str) -> None:
    """A reader (an editor, `uwanja report`) never sees half a file. Windows refuses to replace a file someone has
    open, that attempt is skipped, the next write is the newer anyway."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    try:
        os.replace(temporary, path)
    except PermissionError:
        pass


_STAT = re.compile(r"^(n|sd|tb|h|mb|wv)=|^[+-]?(M?\d|/)|^\d+(\.\d+)?s$|^book$")


def reason_of(comment: str | None) -> str | None:
    """How a game ended from its final move's comment, `+M1/1 0.000s, White mates` is `White mates`; None for a
    comment that's only numbers."""
    if not comment or ", " not in comment:
        return None
    last = comment.rsplit(", ", 1)[1].strip()
    return None if _STAT.match(last) else last


class FileFollower:
    """The lines of a file as it grows. A file that shrinks was started over and is read again from the top."""

    def __init__(self, path: Path):
        self.path = path
        self.position = 0
        self.partial = b""

    def read(self) -> list[str]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.position:
            self.position, self.partial = 0, b""
        if size == self.position:
            return []
        with open(self.path, "rb") as handle:
            handle.seek(self.position)
            chunk = handle.read(1 << 22)
        self.position += len(chunk)
        *lines, self.partial = (self.partial + chunk).split(b"\n")
        return [line.decode("utf-8", errors="replace").rstrip("\r") for line in lines]


class PgnFollower:
    """Games out of a PGN that's being appended to. fastchess writes a game in one go, so a game is complete when
    the next one starts, or when the file has stopped growing."""

    def __init__(self, path: Path):
        self.follower = FileFollower(path)
        self.buffer: list[str] = []

    def read(self) -> list[tuple]:
        lines = self.follower.read()
        games: list[tuple] = []
        for line in lines:
            if line.startswith("[Event ") and self._has_moves():
                games += self._flush()
            self.buffer.append(line)
        if not lines and self._has_moves():
            games += self._flush()
        return games

    def _has_moves(self) -> bool:
        return any(entry.strip() and not entry.startswith("[") for entry in self.buffer)

    def _flush(self) -> list[tuple]:
        text, self.buffer = "\n".join(self.buffer), []
        return list(read_games(text))


class Session:
    def __init__(self, gauntlet: Gauntlet, identity: Identity, paths: RunPaths, reporter: Reporter | None,
                 log: Callable[[str], None] | None = None):
        self.gauntlet = gauntlet
        self.identity = identity
        self.paths = paths
        self.reporter = reporter
        self.log = log or (lambda text: print(text, file=sys.stderr))
        self.results = Results(identity.subject_id, [opponent["id"] for opponent in identity.opponents])
        self.tracker = Tracker(on_update=self._game_updated)
        self.schedule = Schedule(identity.subject_id, [opponent["id"] for opponent in identity.opponents],
                                 gauntlet.rounds)
        self.total_games = len(self.schedule.entries)
        self._entries: dict[int, Entry] = {}          # the tracker's game number -> its place in the schedule
        self._out_follow = FileFollower(paths.out)
        self._log_follow = FileFollower(paths.log)
        self._pgn_follow = PgnFollower(paths.pgn)
        self._dirty = True
        self._last_document = 0.0
        self.finished_marker = False
        paths.games.mkdir(parents=True, exist_ok=True)

    # --- callbacks ---
    def _entry(self, number: int, white: str | None, black: str | None) -> Entry:
        """The schedule's entry for a tracked game: the first one nobody has taken where these two play these
        colours, fastchess starts a pair's games in order."""
        entry = self._entries.get(number)
        if entry is None:
            entry = self._entries[number] = self.schedule.bind(white, black)
        return entry

    def _game_updated(self, game: Game) -> None:
        if not (game.white and game.black):
            return          # the log hasn't said who plays yet, a moment after the first position
        entry = self._entry(game.number, game.white, game.black)
        if entry.state == "finished":
            return
        if entry.state == "scheduled":
            entry.state = "playing"
            self._dirty = True
        fields = set(self.gauntlet.pgn_options)
        pgn = live_pgn(game, self.identity.name, self.gauntlet.tc, self.tracker.engine_ids, round_=entry.round,
                        fields=fields)
        write_atomic(self.paths.games / f"{entry.number}.pgn", pgn)
        if self.reporter:
            self.reporter.put_game(entry.number, pgn, moves=live_moves(game, fields))

    def _game_finished(self, found: FinishedGame) -> None:
        """A game fastchess has written to its PGN: its slot in the schedule is its round and colours, whatever the
        log did or didn't show."""
        entry = self.schedule.locate(found.round, found.white, found.black)
        reason = reason_of(found.comment) or (found.termination if found.termination != "normal" else None)
        if entry.state != "finished":
            self.schedule.finished(entry, found.result, reason)
        elif entry.termination is None and reason:
            entry.termination = reason
        self._dirty = True
        write_atomic(self.paths.games / f"{entry.number}.pgn", found.text)
        if self.reporter:
            self.reporter.put_game(entry.number, found.text, final=True)
        if self.results.add(found.white, found.black, found.result, found.termination, found.comment, found.round):
            totals = self.results.totals()
            self.log(f"uwanja: game {entry.number} {found.white} - {found.black} {found.result}"
                     f"{' (' + entry.termination + ')' if entry.termination else ''}"
                     f"  [{totals['games']}/{self.total_games}]")

    def _runner_line(self, line: str) -> None:
        """What fastchess printed: a game started or finished, with its number and, at the end, why."""
        found = STARTED.match(line)
        if found:
            if self.schedule.started(int(found[1]), found[3], found[4]):
                self._dirty = True
            return
        found = FINISHED.match(line)
        if found:
            entry = self.schedule.get(int(found[1]))
            if entry is not None and entry.state != "finished":
                self.schedule.finished(entry, found[4], found[5])
                self._dirty = True

    # --- the loop ---
    def pump(self) -> bool:
        """Reads what fastchess has written since the last call. True when there was something."""
        printed = self._out_follow.read()
        for line in printed:
            self._runner_line(line)
        lines = self._log_follow.read()
        for line in lines:
            event = parse_fastchess_line(line)
            if event is not None:
                self.tracker.handle(event)
            elif "fastchess --- Finished match" in line:
                self.finished_marker = True
        games = self._pgn_follow.read()
        for game, text in games:
            self._game_finished(to_finished(game, text))
        return bool(printed or lines or games)

    def publish(self, state: str = "running", force: bool = False, final: bool = False) -> None:
        """Writes the match document and hands it to the sender, at most every few seconds unless forced. The final
        one is sent after every game handed over before it."""
        now = time.monotonic()
        stale = now - self._last_document
        if not force and not ((self._dirty and stale >= DOCUMENT_SECONDS) or stale >= HEARTBEAT_SECONDS):
            return
        document = self.identity.document(self.results, state, now_iso(), self.schedule)
        write_atomic(self.paths.match, json.dumps(document, indent=2) + "\n")
        if self.reporter:
            self.reporter.put_match(document, final=final)
        self._dirty, self._last_document = False, now

    def run(self, argv: list[str] | None, idle_exit: float | None = None) -> int:
        with stop_on_signals():
            return self._run(argv, idle_exit)

    def _run(self, argv: list[str] | None, idle_exit: float | None) -> int:
        """Plays the gauntlet (`argv` is fastchess' command line), or with None only follows the files already
        there, for a recorded gauntlet or a replay. Returns the exit code: 0 finished, 130 stopped, 1 error."""
        process: subprocess.Popen | None = None
        out = None
        state = "running"
        if self.reporter:
            self.reporter.start()
        self.publish(force=True)
        try:
            if argv is not None:
                out = open(self.paths.out, "wb")
                process = subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            quiet_since = time.monotonic()
            while True:
                if self.pump():
                    quiet_since = time.monotonic()
                else:
                    time.sleep(POLL_SECONDS)
                self.publish()
                if process is not None and process.poll() is not None:
                    self._drain()
                    state = "finished" if process.returncode == 0 else "error"
                    if state == "error":
                        self.log(f"uwanja: fastchess exited with {process.returncode}, see {self.paths.out}")
                    break
                if process is None and self.finished_marker:
                    self._drain()
                    state = "finished"
                    break
                if process is None and idle_exit and time.monotonic() - quiet_since > idle_exit:
                    state = "stopped"
                    break
        except KeyboardInterrupt:
            state = "stopped"
            if process is not None:
                self.log("uwanja: stopping fastchess")
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
            self._drain()
        finally:
            if out is not None:
                out.close()
        self.publish(state, force=True, final=True)
        if self.reporter:
            self.reporter.close()
        totals = self.results.totals()
        self.log(f"uwanja: {state}, {totals['games']} games, +{totals['wins']} ={totals['draws']} -{totals['losses']}"
                 f"  ({self.paths.root})")
        return {"finished": 0, "stopped": 130}.get(state, 1)

    def _drain(self) -> None:
        """The last of the files, fastchess has exited."""
        for _ in range(5):          # the PGN's last game is released by a read that finds nothing new
            if not self.pump():
                break
