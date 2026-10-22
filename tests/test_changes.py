"""Reports by changes: the digest against the contract's worked example, and a sender against a nyayo that keeps the
contract's rules (docs/match-schema.md, Reports by changes)."""
import gzip
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from uwanja.changes import Held, entry_hash, hex_digest, plan_report, schedule_digest
from uwanja.nyayo import Reporter

# the worked example in nyayo's docs/match-schema.md
ONE = {"number": 1, "round": 1, "opponent": "barbarossa", "subject_colour": "white", "state": "finished",
       "result": "1/2-1/2", "termination": "Draw by 3-fold repetition", "fault": None}
TWO = {"number": 2, "round": 1, "opponent": "barbarossa", "subject_colour": "black", "state": "playing",
       "result": None, "termination": None, "fault": None}
THREE = {"number": 3, "round": 2, "opponent": "belette", "subject_colour": "white", "state": "finished",
         "result": "0-1", "termination": "White loses on time", "fault": {"kind": "timelosses", "side": "subject"}}
TWO_DONE = TWO | {"state": "finished", "result": "0-1", "termination": "Black mates"}


def test_entry_hashes_are_the_contracts():
    assert hex_digest(entry_hash(ONE)) == "c31c00e90ed62959e35278b90fcfb92de04b5f1fa78b01dfcb8e84c01a199ab0"
    assert hex_digest(entry_hash(TWO)) == "0db81474e1538aa80df49d2626e122f6ddabffd0e707b1faebe745c60bb9adb6"
    assert hex_digest(entry_hash(THREE)) == "3c5ba5518743232fd8af04f6f385fb73f39614544606195a51859da121507494"
    assert hex_digest(entry_hash(TWO_DONE)) == "0b3a62f58887d7801725b496b73c802815eb24d20691076bb327b42703b3c425"


def test_the_digest_is_the_contracts_and_follows_a_change_incrementally():
    assert schedule_digest([ONE, TWO, THREE]) == "f2ffb1cc68c680de3609e169daab60a8ce76b49b068aa97f71ec5ca730f04392"
    assert schedule_digest([]) == "0" * 64
    whole = plan_report({"games": [ONE, TWO, THREE]}, None, 1)
    assert whole.whole and whole.digest_hex == "f2ffb1cc68c680de3609e169daab60a8ce76b49b068aa97f71ec5ca730f04392"
    held = whole.accepted(None)
    change = plan_report({"games": [ONE, TWO_DONE, THREE]}, held, 2)
    assert not change.whole and change.document["games"] == [TWO_DONE]
    assert change.document["report"] == {"seq": 2, "base": 1}
    assert change.digest_hex == "f47dc74d0112ddf62cd8c8d94b76c27606366f99e71c1fee292cad4638fa2a01"
    assert change.digest_hex == schedule_digest([ONE, TWO_DONE, THREE])


def test_a_whole_report_says_no_base_and_a_schedule_without_one_is_left_alone():
    assert plan_report({"games": [ONE]}, None, 7).document["report"] == {"seq": 7, "base": None}
    assert plan_report({"match": {}}, None, 1) is None


def test_an_entry_that_vanished_is_a_whole_report():
    held = plan_report({"games": [ONE, TWO]}, None, 1).accepted(None)
    again = plan_report({"games": [ONE]}, held, 2)
    assert again.whole and again.document["games"] == [ONE]


def test_changes_since_the_acknowledged_report_not_the_last_one_built():
    """Reports replaced in the queue never reached nyayo: what they changed goes with the next one."""
    held = plan_report({"games": [ONE, TWO]}, None, 1).accepted(None)
    plan_report({"games": [ONE, TWO_DONE]}, held, 2)          # built, never acknowledged
    later = plan_report({"games": [ONE, TWO_DONE, THREE]}, held, 3)
    assert later.document["games"] == [TWO_DONE, THREE] and later.document["report"] == {"seq": 3, "base": 1}


class FakeNyayo:
    """The rules of *Reports by changes* and nothing else. `headers` False is a nyayo from before them."""

    def __init__(self, headers=True):
        self.reports: list[dict] = []
        self.sizes: list[int] = []
        self.seq = None
        self.schedule: dict[int, dict] = {}
        self.headers = headers
        self.lie_about_digest = False
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_PUT(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if "/games/" in self.path:
                    code, extra = 200, {}
                else:
                    code, extra = fake.report(json.loads(gzip.decompress(body)), len(body))
                self.send_response(code)
                for name, value in extra.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def report(self, document, size):
        with self.lock:
            self.reports.append(document)
            self.sizes.append(size)
            report = document.get("report")
            if report is not None and report["base"] is not None and report["base"] != self.seq:
                return 409, self.said()
            if report is None or report["base"] is None:
                self.schedule = {}
            for entry in document.get("games", []):
                self.schedule[entry["number"]] = entry
            self.seq = report["seq"] if report else None
            return 200, self.said()

    def said(self):
        if not self.headers or self.seq is None:
            return {}
        digest = schedule_digest(list(self.schedule.values()))
        return {"Nyayo-Seq": str(self.seq), "Nyayo-Schedule-Digest": "f" * 64 if self.lie_about_digest else digest}

    def wait(self, count, timeout=5.0):
        start = time.monotonic()
        while len(self.reports) < count:
            assert time.monotonic() - start < timeout, f"nyayo got {len(self.reports)} reports, not {count}"
            time.sleep(0.01)


@pytest.fixture
def nyayo():
    fake = FakeNyayo()
    yield fake
    fake.server.shutdown()


def document(entries, state="running"):
    return {"match": {"state": state}, "games": [dict(entry) for entry in entries]}


def send(nyayo, reporter, doc, final=False):
    count = len(nyayo.reports)
    reporter.put_match(doc, final=final)
    nyayo.wait(count + 1)
    time.sleep(0.05)               # the answer is read and held before the next report is put


def reporter_for(nyayo):
    reporter = Reporter(nyayo.url, None, "m1", log=lambda text: None, timeout=5.0)
    reporter.start()
    return reporter


def test_whole_first_then_changes_and_nyayo_ends_up_with_the_schedule(nyayo):
    reporter = reporter_for(nyayo)
    send(nyayo, reporter, document([ONE, TWO, THREE]))
    send(nyayo, reporter, document([ONE, TWO_DONE, THREE]))
    send(nyayo, reporter, document([ONE, TWO_DONE, THREE | {"state": "playing", "result": None}]))
    first, second, third = nyayo.reports
    assert first["report"] == {"seq": 1, "base": None} and len(first["games"]) == 3
    assert second["report"] == {"seq": 2, "base": 1} and second["games"] == [TWO_DONE]
    assert third["report"] == {"seq": 3, "base": 2} and [g["number"] for g in third["games"]] == [3]
    reporter.close()
    assert not reporter.failed


def test_the_last_document_is_whole(nyayo):
    reporter = reporter_for(nyayo)
    send(nyayo, reporter, document([ONE, TWO]))
    send(nyayo, reporter, document([ONE, TWO_DONE]))
    send(nyayo, reporter, document([ONE, TWO_DONE], "finished"), final=True)
    reporter.close()
    last = nyayo.reports[-1]
    assert last["report"]["base"] is None and len(last["games"]) == 2


def test_a_nyayo_that_doesnt_say_its_seq_is_only_sent_whole_documents():
    old = FakeNyayo(headers=False)
    try:
        reporter = reporter_for(old)
        send(old, reporter, document([ONE, TWO]))
        send(old, reporter, document([ONE, TWO_DONE]))
        send(old, reporter, document([ONE, TWO_DONE, THREE]))
        assert all(report["report"]["base"] is None for report in old.reports)
        assert [len(report["games"]) for report in old.reports] == [2, 2, 3]
        reporter.close()
    finally:
        old.server.shutdown()


def test_a_digest_that_differs_makes_the_next_report_whole(nyayo):
    reporter = reporter_for(nyayo)
    send(nyayo, reporter, document([ONE, TWO]))
    nyayo.lie_about_digest = True
    send(nyayo, reporter, document([ONE, TWO_DONE]))             # a change, answered with a digest that isn't ours
    nyayo.lie_about_digest = False
    send(nyayo, reporter, document([ONE, TWO_DONE, THREE]))      # so this one is whole
    send(nyayo, reporter, document([ONE, TWO_DONE, THREE | {"state": "playing", "result": None}]))
    bases = [report["report"]["base"] for report in nyayo.reports]
    assert bases == [None, 1, None, 3] and len(nyayo.reports[2]["games"]) == 3
    reporter.close()


def test_a_409_makes_the_next_report_whole(nyayo):
    reporter = reporter_for(nyayo)
    send(nyayo, reporter, document([ONE, TWO]))
    nyayo.seq = 99                                               # another report came in between, nyayo's seq moved
    send(nyayo, reporter, document([ONE, TWO_DONE]))             # base 1: 409
    send(nyayo, reporter, document([ONE, TWO_DONE, THREE]))
    assert [report["report"]["base"] for report in nyayo.reports] == [None, 1, None]
    assert len(nyayo.reports[2]["games"]) == 3
    reporter.close()


def test_seq_counts_every_report_and_replaced_ones_leave_a_gap_not_a_clash(nyayo):
    reporter = reporter_for(nyayo)
    send(nyayo, reporter, document([ONE, TWO]))
    send(nyayo, reporter, document([ONE, TWO_DONE]))
    seqs = [report["report"]["seq"] for report in nyayo.reports]
    assert seqs == sorted(set(seqs)) and seqs[0] == 1
    reporter.close()
