"""The live path against a recorded fastchess gauntlet (tests/data: 16 games, main vs dev)."""
import json
import re
from pathlib import Path

import chess.pgn

from uwanja.config import parse
from uwanja.document import Identity
from uwanja.engines import Resolved
from uwanja.hostinfo import HostInfo
from uwanja.session import RunPaths, Session

DATA = Path(__file__).parent / "data"


class FakeReporter:
    def __init__(self):
        self.games: dict[str, list[tuple[str, bool]]] = {}
        self.documents: list[dict] = []
        self.started = self.closed = False

    def start(self): self.started = True
    def close(self): self.closed = True
    def put_match(self, document, final=False): self.documents.append(document)

    def put_game(self, number, pgn, final=False):
        self.games.setdefault(str(number), []).append((pgn, final))


def play(tmp_path, log="fc.log", pgn="fc.pgn", subject="dev", opponent="main", rounds=8):
    gauntlet = parse({"name": "recorded", "tc": "3+0.03", "rounds": rounds, "subject": {"cmd": subject},
                      "opponent": [{"cmd": opponent, "rating": {"value": 2000, "list": "test"}}]})
    engines = [Resolved(spec, Path(spec.cmd), Path("."), {"kind": "recorded"}, f"Elephant Gambit 0.14.7-{spec.id}")
               for spec in gauntlet.engines]
    identity = Identity("test-match", gauntlet, engines[0], engines[1:], HostInfo("windows", "x86_64"), "recorded")
    paths = RunPaths(tmp_path / "run")
    paths.root.mkdir()
    reporter = FakeReporter()
    session = Session(gauntlet, identity, paths, reporter, log=lambda text: None)
    session._log_follow.path, session._pgn_follow.follower.path = DATA / log, DATA / pgn
    code = session.run(None, idle_exit=5)
    return code, session, reporter, paths


def expected_counts():
    wins = draws = losses = 0
    with open(DATA / "fc.pgn", encoding="utf-8") as handle:
        while (game := chess.pgn.read_game(handle)) is not None:
            result, white = game.headers["Result"], game.headers["White"]
            if result == "1/2-1/2":
                draws += 1
            elif (result == "1-0") == (white == "dev"):
                wins += 1
            else:
                losses += 1
    return wins, draws, losses


def test_recorded_gauntlet(tmp_path):
    code, session, reporter, paths = play(tmp_path)
    assert code == 0 and reporter.started and reporter.closed

    document = reporter.documents[-1]
    assert document["match"]["state"] == "finished"
    row = document["results"][0]
    wins, draws, losses = expected_counts()
    assert (row["wins"], row["draws"], row["losses"]) == (wins, draws, losses)
    assert row["games"] == 16 and row["opponent"] == "main"
    assert sum(row["penta"]) == 8                      # eight pairs of two
    assert document["subject"]["name"] == "Elephant Gambit 0.14.7-dev" or document["subject"]["version"]
    assert document["opponents"][0]["rating"] == {"value": 2000.0, "list": "test", "as_of": None}
    assert json.loads(paths.match.read_text(encoding="utf-8"))["match"]["id"] == "test-match"


def test_every_game_streams_move_by_move_and_ends_sealed(tmp_path):
    _, _, reporter, paths = play(tmp_path)
    assert sorted(reporter.games, key=int) == [str(n) for n in range(1, 17)]
    for number, sends in reporter.games.items():
        finals = [pgn for pgn, final in sends if final]
        assert len(finals) == 1 and sends[-1][1], f"game {number}: one final, last"
        assert len(sends) > 5, f"game {number} was sent as it was played"
        assert re.search(r'\[Result "(1-0|0-1|1/2-1/2)"\]', finals[0])
        live = [pgn for pgn, final in sends if not final]
        assert all('[Result "*"]' in pgn for pgn in live)
        # whole game each time: moves only grow
        lengths = [len(chess.pgn.read_game(__import__("io").StringIO(pgn)).end().board().move_stack) for pgn in live]
        assert lengths == sorted(lengths)
    assert (paths.games / "1.pgn").exists()


def comments(pgn_text):
    """[(move, comment)] of a PGN's first game."""
    game = chess.pgn.read_game(__import__("io").StringIO(pgn_text))
    return [(node.move.uci(), node.comment) for node in game.mainline()]


def split_comment(text):
    """(score/depth, seconds, the extras after the time without the game's closing reason)"""
    head, _, rest = text.partition(", ")
    score, seconds = head.split(" ")
    extras = re.findall(r'(?:n|nps|sd|hashfull|tbhits)=\d+|pv="[^"]*"', rest)
    return score, float(seconds.rstrip("s")), extras


def test_live_comments_are_what_fastchess_writes(tmp_path):
    """fastchess run with -pgnout nodes=true nps=true pv=true, its log and PGN (tests/data/fc-opts.*): the live PGN
    rebuilt from the log has the same score, depth, nodes, nps and pv on every move, the time to the millisecond."""
    _, _, reporter, _ = play(tmp_path, "fc-opts.log", "fc-opts.pgn", "a", "b", rounds=2)
    finals = {}
    with open(DATA / "fc-opts.pgn", encoding="utf-8") as handle:
        text = handle.read()
    for chunk in re.split(r"\n(?=\[Event )", text):
        game = chess.pgn.read_game(__import__("io").StringIO(chunk))
        finals[(game.headers["White"], game.headers["Black"], game.headers.get("FEN"))] = comments(chunk)
    checked = 0
    for number, sends in reporter.games.items():
        live = [pgn for pgn, final in sends if not final][-1]
        ours = comments(live)
        head = chess.pgn.read_game(__import__("io").StringIO(live)).headers
        theirs = finals[(head["White"], head["Black"], head.get("FEN"))]
        assert len(ours) >= len(theirs) - 2, f"game {number}: the live PGN has the game up to its last move or two"
        for (move, mine), (their_move, expected) in zip(ours, theirs):
            assert move == their_move
            score, seconds, extras = split_comment(mine)
            e_score, e_seconds, e_extras = split_comment(expected)
            assert (score, extras) == (e_score, e_extras), f"{mine!r} != {expected!r}"
            assert abs(seconds - e_seconds) <= 0.005, f"{mine!r} != {expected!r}"
            checked += 1
    assert checked > 100
    assert 'pv="' in live and "nps=0" in live and ", n=" in live


def test_the_whole_schedule_goes_first_and_ends_finished(tmp_path, monkeypatch):
    monkeypatch.setattr("uwanja.session.DOCUMENT_SECONDS", 0)      # a replay is over in a blink, publish every change
    _, _, reporter, _ = play(tmp_path)
    first, last = reporter.documents[0], reporter.documents[-1]
    assert first["match"]["state"] == "running"
    assert first["progress"] == {"total": 16, "played": 0, "playing": 0}
    assert [g["state"] for g in first["games"]] == ["scheduled"] * 16
    assert [(g["number"], g["subject_colour"]) for g in first["games"][:2]] == [(1, "white"), (2, "black")]
    assert last["progress"] == {"total": 16, "played": 16, "playing": 0}
    assert all(g["state"] == "finished" and g["result"] for g in last["games"])
    assert all(g["termination"] for g in last["games"])          # "White mates", "Draw by ..."
    # a game in the middle was seen playing
    assert any(d["progress"]["playing"] > 0 for d in reporter.documents)


def test_fastchess_stdout_drives_the_state(tmp_path):
    gauntlet = parse({"name": "x", "tc": "1+1", "rounds": 1, "subject": {"cmd": "a"}, "opponent": [{"cmd": "b"}]})
    engines = [Resolved(spec, Path(spec.cmd), Path("."), {}, spec.id) for spec in gauntlet.engines]
    identity = Identity("m", gauntlet, engines[0], engines[1:], HostInfo("linux", "x86_64"), "r")
    paths = RunPaths(tmp_path)
    session = Session(gauntlet, identity, paths, None, log=lambda text: None)
    paths.out.write_text("Indexing opening suite...\nStarted game 1 of 2 (a vs b)\n", encoding="utf-8")
    session.pump()
    assert session.schedule.progress() == {"total": 2, "played": 0, "playing": 1}
    with open(paths.out, "a", encoding="utf-8") as handle:
        handle.write("Finished game 1 (a vs b): 1-0 {Black makes an illegal move: e2e5}\n")
    session.pump()
    entry = session.schedule.get(1)
    assert (entry.state, entry.result, entry.fault) == ("finished", "1-0", {"kind": "illegal", "side": "opponent"})


def test_results_do_not_wait_for_the_log(tmp_path):
    """The October CCRL gauntlet: a result whose game the log hadn't finished with, more than a few seconds, was given
    a new number and took a later slot, and 37 phantom games came out beyond 1400. The PGN alone places every game."""
    (tmp_path / "empty.log").write_text("", encoding="utf-8")
    _, session, reporter, _ = play(tmp_path, log=str(tmp_path / "empty.log"))
    numbers = sorted(int(n) for n in reporter.games)
    assert numbers == list(range(1, 17))
    assert len(session.schedule.entries) == 16
    assert all(e.state == "finished" for e in session.schedule.entries)
    assert session.results.totals()["games"] == 16


def test_rebuild_puts_a_run_right_from_its_pgn(tmp_path):
    import shutil
    from uwanja.rebuild import rebuild
    _, session, reporter, paths = play(tmp_path)
    shutil.copy2(DATA / "fc.pgn", paths.pgn)
    paths.config.write_text('tc = "3+0.03"\nrounds = 8\n[subject]\ncmd = "dev"\n[[opponent]]\ncmd = "main"\n', encoding="utf-8")
    paths.identity.write_text(json.dumps(session.identity.to_json()), encoding="utf-8")
    # a bad run: an extra live file past the schedule, and a slot left playing
    (paths.games / "17.pgn").write_text('[Event "x"]\n[Result "*"]\n\n*\n', encoding="utf-8")
    found = rebuild(paths.root)
    assert (found.games, found.entries, found.duplicates) == (16, 16, 0)
    assert sorted(int(p.stem) for p in paths.games.glob("*.pgn")) == list(range(1, 17))
    assert (paths.root / "games.before-rebuild" / "17.pgn").exists()
    assert found.document["progress"] == {"total": 16, "played": 16, "playing": 0}
    assert found.document["results"][0]["games"] == 16
