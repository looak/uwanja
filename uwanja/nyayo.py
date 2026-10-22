"""The nyayo client: the match document and every game, sent in the background.

A producer's reports are all "the whole truth so far" (match-schema.md), so a lost one costs nothing: the sender keeps
only the newest document and the newest PGN of each game, and a request that fails because nyayo is away is tried
again with whatever is newest by then. Nothing here can slow the match.

The match document and the games have a thread each, each with its own requests, so a report never waits behind a
round's games and a slow or failing game route leaves the reports going (ramani plans/live-updates.md, milestone 1).
Games wait for the match only where nyayo needs it: until it has answered a document, and again when it answers a
game with 404. The last document of a run waits for the games, so nyayo's final state comes after every game.

The schedule goes by changes (changes.py) once an answer has carried `Nyayo-Seq`: the report says its seq and the
base, the seq nyayo last acknowledged, and carries the entries that changed since. Whole documents go first, after
anything unexpected (a refusal, a failed request, an answer without the headers or with another digest than ours),
at the end of a run and from `uwanja report`.
"""
from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from .changes import Held, plan_report


@dataclass
class Answer:
    """What nyayo answered: the status and the headers (names lowercased) that say what it holds."""
    status: int
    headers: dict[str, str]

    @property
    def seq(self) -> int | None:
        value = self.headers.get("nyayo-seq", "")
        return int(value) if value.isdigit() else None

    @property
    def digest(self) -> str | None:
        return self.headers.get("nyayo-schedule-digest")


def nyayo_config() -> tuple[str | None, str | None]:
    """URL and token from the environment (NYAYO_URL, NYAYO_TOKEN), else git config (nyayo.url, nyayo.token), the
    way results.py takes them."""
    def git(key: str) -> str | None:
        try:
            result = subprocess.run(["git", "config", "--get", key], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.strip() or None if result.returncode == 0 else None

    url = os.environ.get("NYAYO_URL") or git("nyayo.url")
    token = os.environ.get("NYAYO_TOKEN") or git("nyayo.token")
    return (url.rstrip("/") if url else None), token


class Reporter:
    def __init__(self, url: str | None, token: str | None, match_id: str, log: Callable[[str], None] | None = None,
                 timeout: float = 15.0):
        self.url, self.token, self.match_id = url, token, match_id
        self.log = log or (lambda text: print(text, file=sys.stderr))
        self.timeout = timeout
        self._lock = threading.Condition()
        self._match: dict | None = None         # the newest document not yet sent, an older one is replaced
        self._final = False                     # it's the run's last, it goes once the games have
        self._match_busy = False
        self._seq = 0                           # the last report's number, 1 for the first of the run
        self._held: Held | None = None          # the schedule nyayo holds, None until an answer said it has a seq
        self._last_sent: dict | None = None     # sent again when a game finds nyayo doesn't know the match
        self._known = False                     # nyayo has answered a document: games may go
        self._refused = False                   # ... and the answer was a refusal
        self._games: dict[str, str] = {}
        self._games_busy = False
        self._sealed: set[str] = set()          # games whose final PGN went (or is queued), nothing later replaces it
        self._release = False                   # closing, the games are done or out of time: the last document goes
        self._stop = False
        self._threads: list[threading.Thread] = []
        self.sent = {"match": 0, "games": 0}
        self.bytes = {"match": 0, "games": 0}   # the bodies as they went, compressed
        self._failed: dict[str, str | None] = {"match": None, "games": None}   # the last error of each path

    @property
    def failed(self) -> str | None:
        """The last error still standing, for the run's summary."""
        return self._failed["match"] or self._failed["games"]

    @property
    def enabled(self) -> bool:
        return self.url is not None

    def start(self) -> None:
        if self.enabled and not self._threads:
            self._threads = [threading.Thread(target=self._run_match, name="nyayo-match", daemon=True),
                             threading.Thread(target=self._run_games, name="nyayo-games", daemon=True)]
            for thread in self._threads:
                thread.start()

    def put_match(self, document: dict, final: bool = False) -> None:
        """`final`: the run's last document, sent after the games queued before it."""
        with self._lock:
            self._match = document
            self._final = self._final or final
            self._lock.notify_all()

    def put_game(self, number: int | str, pgn: str, final: bool = False) -> None:
        key = str(number)
        with self._lock:
            if key in self._sealed:
                return
            self._games[key] = pgn
            if final:
                self._sealed.add(key)
            self._lock.notify_all()

    def close(self, timeout: float = 30.0) -> None:
        """Waits for what's queued to go, the games and then the last document, up to `timeout`. The last document is
        tried even when the games ran out of time: nyayo showing `running` forever is worse than a game missing."""
        if not self._threads:
            return
        deadline = time.monotonic() + timeout
        with self._lock:
            while (self._games or self._games_busy) and time.monotonic() < deadline:
                self._lock.wait(0.2)
            self._release = True
            self._lock.notify_all()
            deadline = max(deadline, time.monotonic() + self.timeout)
            while (self._match is not None or self._match_busy) and time.monotonic() < deadline:
                self._lock.wait(0.2)
            self._stop = True
            self._lock.notify_all()
        for thread in self._threads:
            thread.join(timeout=self.timeout + 2)    # a request may be on its way
        if self._match is not None or self._games:
            self.log(f"uwanja: nyayo didn't take everything ({len(self._games)} games"
                     f"{', the match' if self._match else ''} left), `uwanja report <run dir>` sends it again")

    # --- the match document ---
    def _match_ready(self) -> bool:
        """A document waits only when it's the last one and games are still going to a match nyayo knows."""
        if self._match is None:
            return False
        return not self._final or self._release or not self._known or not (self._games or self._games_busy)

    def _run_match(self) -> None:
        backoff = 1.0
        while True:
            with self._lock:
                while not self._stop and not self._match_ready():
                    self._lock.wait()
                if self._stop:
                    return
                match, final, self._match, self._final = self._match, self._final, None, False
                early = final and not self._release and bool(self._games or self._games_busy)
                self._match_busy = True
            answer = self._put_match(match, whole=final)
            status = answer.status if answer else None
            with self._lock:
                self._match_busy = False
                if status is None:
                    if self._match is None:             # unless a newer one came meanwhile
                        self._match, self._final = match, self._final or final
                else:
                    self._known, self._refused = True, status >= 300
                    if not self._refused:
                        self.sent["match"] += 1
                        self._last_sent = match
                    if early and self._match is None:   # it went ahead of the games for one of them, and goes again
                        self._match, self._final = match, True
                self._lock.notify_all()
            if status is None:
                time.sleep(backoff)
                backoff = min(backoff * 2, 15.0)
            else:
                backoff = 1.0

    def _put_match(self, match: dict, whole: bool) -> Answer | None:
        """One report: the changes since nyayo's last acknowledged one when it has told its seq, else whole."""
        with self._lock:
            self._seq += 1
            plan = plan_report(match, None if whole else self._held, self._seq)
            if plan is not None:
                match = plan.document
        body = json.dumps(match, separators=(",", ":")).encode("utf-8")
        answer = self._put("", body, "application/json", gz=True)
        with self._lock:
            if plan is None:
                self._held = None
            elif answer is not None and 200 <= answer.status < 300 and answer.seq == plan.seq:
                if answer.digest == plan.digest_hex:
                    self._held = plan.accepted(self._held)
                else:
                    self._held = None
                    held = (answer.digest or "none")[:12]
                    self._note("match", f"nyayo's schedule differs from ours (digest {held}), the next report is whole")
            else:
                self._held = None       # a refusal, a failure, or an answer without a seq: whole next
        return answer

    # --- the games ---
    def _run_games(self) -> None:
        backoff = 1.0
        while True:
            with self._lock:
                while not self._stop and not (self._games and self._known):
                    self._lock.wait()
                if self._stop:
                    return
                games = list(self._games.items())
                self._games.clear()
                self._games_busy = True
            failed = False
            for index, (key, pgn) in enumerate(games):
                answer = self._put(f"/games/{urllib.parse.quote(key, safe='')}", pgn.encode("utf-8"),
                                   "application/x-chess-pgn", gz=False)
                status = answer.status if answer else None
                if status == 404 and not self._refused:
                    self._unknown_match(games[index:])
                    break
                if status is None:
                    failed = True
                    with self._lock:
                        self._games.setdefault(key, pgn)    # unless a newer one came meanwhile
                elif status < 300 or status == 409:         # 409: sealed already, nothing to resend
                    self.sent["games"] += 1
                elif status == 404:
                    self._note("games", "nyayo refused the match, so it has no games either")
            with self._lock:
                self._games_busy = False
                self._lock.notify_all()
            if failed:
                time.sleep(backoff)
                backoff = min(backoff * 2, 15.0)
            else:
                backoff = 1.0

    def _unknown_match(self, games: list[tuple[str, str]]) -> None:
        """nyayo doesn't know the match (not yet, or not any more): the match goes first, these games after it."""
        with self._lock:
            for key, pgn in games:
                self._games.setdefault(key, pgn)
            self._known = False
            self._held = None
            if self._match is None and not self._match_busy and self._last_sent is not None:
                self._match = self._last_sent
            self._lock.notify_all()

    def _put(self, suffix: str, body: bytes, content_type: str, gz: bool) -> Answer | None:
        """nyayo's answer, a refusal's too (not tried again); None when it's unreachable or failing (tried again)."""
        path = "games" if suffix else "match"
        headers = {"Content-Type": content_type}
        if gz:
            body, headers["Content-Encoding"] = gzip.compress(body), "gzip"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self.bytes[path] += len(body)
        url = f"{self.url}/api/matches/{urllib.parse.quote(self.match_id, safe='')}{suffix}"
        request = urllib.request.Request(url, data=body, method="PUT", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                self._failed[path] = None
                return Answer(response.status, {name.lower(): value for name, value in response.headers.items()})
        except urllib.error.HTTPError as error:
            reason = error.read(300).decode("utf-8", "replace").strip()
            if error.code >= 500:
                self._note(path, f"HTTP {error.code} from nyayo: {reason}")
                return None
            if error.code != 404 or not suffix:     # a game's 404 is the match not there (yet), it's sent first
                self._note(path, f"nyayo refused {suffix or 'the match'}: HTTP {error.code} {reason}")
            return Answer(error.code, {name.lower(): value for name, value in error.headers.items()})
        except (urllib.error.URLError, OSError) as error:
            self._note(path, f"nyayo unreachable: {error}")
            return None

    def _note(self, path: str, text: str) -> None:
        if text != self._failed[path]:      # once per kind of trouble on each path, not once per move
            self.log(f"uwanja: {text}")
        self._failed[path] = text
