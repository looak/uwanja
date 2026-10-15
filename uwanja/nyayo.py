"""The nyayo client: the match document and every game, sent in the background.

A producer's reports are all "the whole truth so far" (match-schema.md), so a lost one costs nothing: the sender keeps
only the newest document and the newest PGN of each game, and a request that fails because nyayo is away is tried
again with whatever is newest by then. Nothing here can slow the match, the sender is its own thread.
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
from typing import Callable


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
        self._match: dict | None = None
        self._games: dict[str, str] = {}
        self._sealed: set[str] = set()          # games whose final PGN went (or is queued), nothing later replaces it
        self._stop = False
        self._thread: threading.Thread | None = None
        self.sent = {"match": 0, "games": 0}
        self.failed: str | None = None          # the last error, for the run's summary

    @property
    def enabled(self) -> bool:
        return self.url is not None

    def start(self) -> None:
        if self.enabled and self._thread is None:
            self._thread = threading.Thread(target=self._run, name="nyayo", daemon=True)
            self._thread.start()

    def put_match(self, document: dict) -> None:
        with self._lock:
            self._match = document
            self._lock.notify()

    def put_game(self, number: int | str, pgn: str, final: bool = False) -> None:
        key = str(number)
        with self._lock:
            if key in self._sealed:
                return
            self._games[key] = pgn
            if final:
                self._sealed.add(key)
            self._lock.notify()

    def close(self, timeout: float = 30.0) -> None:
        """Waits for what's queued to go, up to `timeout`."""
        if self._thread is None:
            return
        deadline = time.monotonic() + timeout
        with self._lock:
            while (self._match is not None or self._games) and time.monotonic() < deadline:
                self._lock.wait(0.2)
            self._stop = True
            self._lock.notify()
        self._thread.join(timeout=self.timeout + 2)    # a request may be on its way
        if self._match is not None or self._games:
            self.log(f"uwanja: nyayo didn't take everything ({len(self._games)} games"
                     f"{', the match' if self._match else ''} left), `uwanja report <run dir>` sends it again")

    # --- the sender ---
    def _run(self) -> None:
        backoff = 1.0
        while True:
            with self._lock:
                while not self._stop and self._match is None and not self._games:
                    self._lock.wait()
                if self._stop and self._match is None and not self._games:
                    return
                games = dict(self._games)
                match, self._match = self._match, None
                self._games.clear()
            failed = False
            for key, pgn in games.items():
                outcome = self._put(f"/games/{urllib.parse.quote(key, safe='')}", pgn.encode("utf-8"),
                                    "application/x-chess-pgn", gz=False)
                if outcome is None:
                    failed = True
                    with self._lock:
                        self._games.setdefault(key, pgn)    # unless a newer one came meanwhile
                else:
                    self.sent["games"] += 1
            if match is not None:
                body = json.dumps(match, separators=(",", ":")).encode("utf-8")
                if self._put("", body, "application/json", gz=True) is None:
                    failed = True
                    with self._lock:
                        if self._match is None:
                            self._match = match
                else:
                    self.sent["match"] += 1
            with self._lock:
                self._lock.notify_all()
            if failed:
                if self._stop:
                    return
                time.sleep(backoff)
                backoff = min(backoff * 2, 15.0)
            else:
                backoff = 1.0

    def _put(self, suffix: str, body: bytes, content_type: str, gz: bool) -> bool | None:
        """True sent, False refused (don't try again), None unreachable or a server error (try again)."""
        headers = {"Content-Type": content_type}
        if gz:
            body, headers["Content-Encoding"] = gzip.compress(body), "gzip"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        url = f"{self.url}/api/matches/{urllib.parse.quote(self.match_id, safe='')}{suffix}"
        request = urllib.request.Request(url, data=body, method="PUT", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout):
                self.failed = None
                return True
        except urllib.error.HTTPError as error:
            reason = error.read(300).decode("utf-8", "replace").strip()
            if error.code >= 500:
                self._note(f"HTTP {error.code} from nyayo: {reason}")
                return None
            self._note(f"nyayo refused {suffix or 'the match'}: HTTP {error.code} {reason}")
            return True if error.code == 409 and suffix else False   # a sealed game, nothing to resend
        except (urllib.error.URLError, OSError) as error:
            self._note(f"nyayo unreachable: {error}")
            return None

    def _note(self, text: str) -> None:
        if text != self.failed:             # once per kind of trouble, not once per move
            self.log(f"uwanja: {text}")
        self.failed = text
