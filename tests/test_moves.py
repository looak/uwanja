"""Moves by changes: the sender against a nyayo that keeps the contract's rules for `PATCH .../games/{game}`
(docs/match-schema.md, Moves by changes), and a 503 with Retry-After."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from uwanja.nyayo import Reporter


def moves_of(count, start=0):
    return [(f"m{n}", f"+0.{n % 10}0/9 0.1s") for n in range(start + 1, start + count + 1)]


def pgn_of(moves):
    return " ".join(san for san, _ in moves)       # the stand-in nyayo counts a PGN's plies by its words


class FakeNyayo:
    def __init__(self, headers=True):
        self.requests: list[tuple] = []            # (method, game, what it carried)
        self.plies: dict[str, int] = {}
        self.sealed: set[str] = set()
        self.headers = headers
        self.busy = 0                              # the next n game requests are 503
        self.retry_after: str | None = None
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def handle_request(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                code, extra = fake.answer(self.command, self.path, body)
                self.send_response(code)
                for name, value in extra.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_PUT = do_PATCH = handle_request

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def said(self, game):
        return {"Nyayo-Plies": str(self.plies.get(game, 0))} if self.headers else {}

    def answer(self, method, path, body):
        if "/games/" not in path:
            return 200, {}
        game = path.rsplit("/", 1)[1]
        with self.lock:
            if self.busy:
                self.busy -= 1
                self.requests.append((method, game, "busy", time.monotonic()))
                return 503, ({"Retry-After": self.retry_after} if self.retry_after else {})
            if method == "PUT":
                text = body.decode("utf-8")
                self.requests.append(("PUT", game, text, time.monotonic()))
                if game in self.sealed:
                    return 409, self.said(game)
                self.plies[game] = len(text.split())
                if text.endswith(" 1-0"):
                    self.sealed.add(game)
                return 200, self.said(game)
            change = json.loads(body)
            self.requests.append(("PATCH", game, change, time.monotonic()))
            if game in self.sealed or change["from"] != self.plies.get(game, 0) or game not in self.plies:
                return 409, self.said(game)
            self.plies[game] += len(change["moves"])
            return 200, self.said(game)

    def calls(self, game="1"):
        with self.lock:
            return [(method, what) for method, g, what, *_ in self.requests if g == game and what != "busy"]

    def wait_plies(self, count, game="1", timeout=5.0):
        start = time.monotonic()
        while self.plies.get(game) != count:
            assert time.monotonic() - start < timeout, f"nyayo holds {self.plies.get(game)} plies, not {count}"
            time.sleep(0.01)


@pytest.fixture
def nyayo():
    fake = FakeNyayo()
    yield fake
    fake.server.shutdown()


def reporter_for(nyayo):
    reporter = Reporter(nyayo.url, None, "m1", log=lambda text: None, timeout=5.0)
    reporter.start()
    reporter.put_match({"n": 0})
    return reporter


def move(reporter, nyayo, count, game="1"):
    moves = moves_of(count)
    reporter.put_game(game, pgn_of(moves), moves=moves)
    nyayo.wait_plies(count, game)
    time.sleep(0.05)               # the answer is read before the next state is put


def test_whole_first_then_only_the_moves_nyayo_hasnt_got(nyayo):
    reporter = reporter_for(nyayo)
    for count in (1, 2, 3, 5):
        move(reporter, nyayo, count)
    calls = nyayo.calls()
    assert calls[0] == ("PUT", "m1")
    assert [what["from"] for method, what in calls[1:]] == [1, 2, 3]
    assert [[m["san"] for m in what["moves"]] for _, what in calls[1:]] == [["m2"], ["m3"], ["m4", "m5"]]
    assert calls[-1][1]["moves"][-1] == {"san": "m5", "comment": moves_of(5)[-1][1]}
    reporter.close()


def test_moves_made_while_a_request_is_on_its_way_go_in_one_request(nyayo):
    reporter = reporter_for(nyayo)
    move(reporter, nyayo, 1)
    for count in range(2, 8):                 # six states, the sender keeps the newest
        moves = moves_of(count)
        reporter.put_game("1", pgn_of(moves), moves=moves)
    nyayo.wait_plies(7)
    patches = [what for method, what in nyayo.calls() if method == "PATCH"]
    assert patches[0]["from"] == 1 and len(patches) < 6
    assert sum(len(patch["moves"]) for patch in patches) == 6
    reporter.close()


def test_a_move_costs_the_same_at_ply_300_as_at_ply_3(nyayo):
    reporter = reporter_for(nyayo)
    move(reporter, nyayo, 3)
    move(reporter, nyayo, 4)
    early = nyayo.calls()[-1][1]
    move(reporter, nyayo, 300)
    move(reporter, nyayo, 301)
    late = nyayo.calls()[-1][1]
    assert abs(len(json.dumps(early)) - len(json.dumps(late))) < 8      # one move, wherever in the game
    assert len(late["moves"]) == 1 and late["from"] == 300
    reporter.close()


def test_a_409_makes_the_next_request_the_whole_pgn_and_changes_go_on_after(nyayo):
    reporter = reporter_for(nyayo)
    move(reporter, nyayo, 2)
    nyayo.plies["1"] = 1                       # nyayo lost a ply, or another run wrote to the game
    moves = moves_of(3)
    reporter.put_game("1", pgn_of(moves), moves=moves)
    nyayo.wait_plies(3)
    time.sleep(0.05)
    assert [method for method, _ in nyayo.calls()] == ["PUT", "PATCH", "PUT"]
    move(reporter, nyayo, 4)
    assert [method for method, _ in nyayo.calls()] == ["PUT", "PATCH", "PUT", "PATCH"]
    reporter.close()


def test_a_game_that_started_over_or_changed_is_the_whole_pgn(nyayo):
    reporter = reporter_for(nyayo)
    move(reporter, nyayo, 4)
    move(reporter, nyayo, 2)                   # shorter: a game that started over
    other = [(f"x{n}", "c") for n in range(1, 6)]
    reporter.put_game("1", pgn_of(other), moves=other)   # longer, but not the same moves
    nyayo.wait_plies(5)
    assert [method for method, _ in nyayo.calls()] == ["PUT", "PUT", "PUT"]
    reporter.close()


def test_the_sealed_game_is_the_whole_final_pgn(nyayo):
    reporter = reporter_for(nyayo)
    move(reporter, nyayo, 3)
    final = pgn_of(moves_of(5)) + " 1-0"
    reporter.put_game("1", final, final=True, moves=moves_of(5))
    reporter.close()
    assert nyayo.calls()[-1] == ("PUT", final)
    assert "1" in nyayo.sealed


def test_without_nyayo_plies_only_whole_pgns_are_sent():
    old = FakeNyayo(headers=False)
    try:
        reporter = reporter_for(old)
        for count in (1, 2, 3):
            moves = moves_of(count)
            reporter.put_game("1", pgn_of(moves), moves=moves)
            deadline = time.monotonic() + 3
            while len(old.calls()) < count and time.monotonic() < deadline:
                time.sleep(0.01)
        assert [method for method, _ in old.calls()] == ["PUT"] * 3
        reporter.close()
    finally:
        old.server.shutdown()


def test_a_game_without_moves_is_sent_whole_as_by_uwanja_report(nyayo):
    reporter = reporter_for(nyayo)
    move(reporter, nyayo, 2)
    reporter.put_game("1", pgn_of(moves_of(3)))
    nyayo.wait_plies(3)
    assert [method for method, _ in nyayo.calls()] == ["PUT", "PUT"]
    reporter.close()


def test_retry_after_is_what_the_games_thread_waits(nyayo):
    nyayo.busy, nyayo.retry_after = 2, "0.1"
    reporter = reporter_for(nyayo)
    moves = moves_of(2)
    started = time.monotonic()
    reporter.put_game("1", pgn_of(moves), moves=moves)
    nyayo.wait_plies(2, timeout=3.0)
    busy = [when for method, game, what, when in nyayo.requests if what == "busy"]
    assert len(busy) == 2 and time.monotonic() - started < 0.9       # the backoff would have waited 1 s and then 2 s
    assert busy[1] - busy[0] < 0.6
    reporter.close()


def test_without_retry_after_the_backoff_applies(nyayo):
    nyayo.busy = 1
    reporter = reporter_for(nyayo)
    moves = moves_of(1)
    reporter.put_game("1", pgn_of(moves), moves=moves)
    nyayo.wait_plies(1, timeout=4.0)
    busy, ok = nyayo.requests[0][3], nyayo.requests[1][3]
    assert ok - busy >= 0.9
    reporter.close()


def test_match_reports_go_while_the_games_are_told_to_wait(nyayo):
    nyayo.busy, nyayo.retry_after = 1000, "30"
    reporter = reporter_for(nyayo)
    moves = moves_of(1)
    reporter.put_game("1", pgn_of(moves), moves=moves)
    time.sleep(0.2)
    reporter.put_match({"n": 1})
    time.sleep(0.3)
    assert reporter.sent["match"] == 2
    reporter.close(timeout=0.5)
