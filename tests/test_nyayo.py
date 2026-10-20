"""The sender against a nyayo of our own: the match document never waits behind the games, and the run's last
document comes after them."""
import gzip
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from uwanja.nyayo import Reporter


class FakeNyayo:
    """Records every PUT in the order it was answered. `game_delay` and `game_status` make the game routes slow or
    failing; a game of a match it hasn't been sent is a 404, as nyayo answers."""

    def __init__(self):
        self.requests: list[tuple] = []       # ("match", n, final) or ("game", key, pgn)
        self.known = False
        self.game_delay = 0.0
        self.game_status = 200
        self.match_delay = 0.0
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_PUT(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if "/games/" in self.path:
                    code = fake.game(self.path.rsplit("/", 1)[1], body.decode("utf-8"))
                else:
                    code = fake.match(json.loads(gzip.decompress(body)))
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def match(self, document) -> int:
        time.sleep(self.match_delay)
        with self.lock:
            self.known = True
            self.requests.append(("match", document["n"], document.get("final", False)))
        return 200

    def game(self, key, pgn) -> int:
        time.sleep(self.game_delay)
        with self.lock:
            if not self.known:
                return 404
            if self.game_status != 200:
                return self.game_status
            self.requests.append(("game", key, pgn))
        return 200

    def matches(self) -> list[int]:
        with self.lock:
            return [n for kind, n, *_ in self.requests if kind == "match"]

    def games(self) -> list[str]:
        with self.lock:
            return [key for kind, key, *_ in self.requests if kind == "game"]

    def wait(self, condition, timeout: float = 5.0) -> float:
        """Seconds until `condition()` held; fails the test past `timeout`."""
        start = time.monotonic()
        while not condition():
            assert time.monotonic() - start < timeout, f"waited {timeout}s, nyayo got {self.requests}"
            time.sleep(0.01)
        return time.monotonic() - start


@pytest.fixture
def nyayo():
    fake = FakeNyayo()
    yield fake
    fake.server.shutdown()


def reporter_for(nyayo, timeout=15.0) -> Reporter:
    reporter = Reporter(nyayo.url, None, "m1", log=lambda text: None, timeout=timeout)
    reporter.start()
    return reporter


def started(nyayo, reporter):
    """A run's first document, taken: the games may go."""
    reporter.put_match({"n": 0})
    nyayo.wait(lambda: nyayo.matches() == [0])


def test_a_report_isnt_delayed_by_32_live_games(nyayo):
    nyayo.game_delay = 0.1                    # 32 games one after another: 3.2 s
    reporter = reporter_for(nyayo)
    started(nyayo, reporter)
    for number in range(1, 33):
        reporter.put_game(number, f"live {number}")
    nyayo.wait(lambda: reporter._games_busy)
    reporter.put_match({"n": 1})
    assert nyayo.wait(lambda: 1 in nyayo.matches()) < 1.0
    assert len(nyayo.games()) < 32            # it went while the round's games were still going
    reporter.close()
    assert sorted(nyayo.games(), key=int) == [str(n) for n in range(1, 33)]


@pytest.mark.parametrize("trouble", ["failing", "stalled"])
def test_reports_go_while_the_game_path_is_stuck(nyayo, trouble):
    if trouble == "failing":
        nyayo.game_status = 503
    else:
        nyayo.game_delay = 30.0               # longer than the test, the request doesn't come back
    reporter = reporter_for(nyayo, timeout=60.0)
    started(nyayo, reporter)
    for number in range(1, 33):
        reporter.put_game(number, f"live {number}")
    for n in range(1, 4):
        reporter.put_match({"n": n})
        assert nyayo.wait(lambda: n in nyayo.matches(), timeout=2.0) < 1.0
        time.sleep(0.2)
    assert nyayo.games() == []


def test_only_the_newest_document_waits(nyayo):
    nyayo.match_delay = 0.3
    reporter = reporter_for(nyayo)
    reporter.put_match({"n": 1})
    time.sleep(0.1)                           # 1 is on its way
    reporter.put_match({"n": 2})
    reporter.put_match({"n": 3})
    reporter.close()
    assert nyayo.matches() == [1, 3]


def test_the_last_document_goes_after_every_game(nyayo):
    nyayo.game_delay = 0.05
    reporter = reporter_for(nyayo)
    started(nyayo, reporter)
    for number in range(1, 11):
        reporter.put_game(number, f"final {number}", final=True)
    reporter.put_match({"n": 1, "final": True}, final=True)
    reporter.close()
    assert len(nyayo.games()) == 10
    assert nyayo.requests[-1] == ("match", 1, True)


def test_the_last_document_goes_when_the_games_cant(nyayo):
    nyayo.game_status = 503
    reporter = reporter_for(nyayo)
    started(nyayo, reporter)
    reporter.put_game(1, "final 1", final=True)
    reporter.put_match({"n": 1, "final": True}, final=True)
    reporter.close(timeout=1.0)
    assert nyayo.matches() == [0, 1] and reporter.failed


def test_games_wait_for_the_match_nyayo_doesnt_know(nyayo):
    """`uwanja report` hands over the games before the document: the match goes first all the same, and the last
    document again after the games."""
    reporter = reporter_for(nyayo)
    reporter.put_game(1, "final 1", final=True)
    reporter.put_game(2, "final 2", final=True)
    reporter.put_match({"n": 1, "final": True}, final=True)
    reporter.close()
    kinds = [request[:2] for request in nyayo.requests]
    assert kinds[0] == ("match", 1) and kinds[-1] == ("match", 1)
    assert sorted(nyayo.games()) == ["1", "2"]


def test_a_404_for_a_game_sends_the_match_again(nyayo):
    """nyayo lost the match (deleted, restored from an older database): the last document goes again, then the
    game."""
    reporter = reporter_for(nyayo)
    started(nyayo, reporter)
    reporter.put_game(1, "live 1")
    nyayo.wait(lambda: nyayo.games() == ["1"])
    nyayo.known = False
    reporter.put_game(1, "live 1 more")
    nyayo.wait(lambda: nyayo.games() == ["1", "1"])
    assert [request[:2] for request in nyayo.requests] == [("match", 0), ("game", "1"), ("match", 0), ("game", "1")]
    reporter.close()
