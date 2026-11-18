"""Continuing a stopped gauntlet: slots counted once, the missing pairs played on their own openings."""
import json
import sys
import textwrap
from pathlib import Path

from uwanja.config import parse
from uwanja.document import Identity
from uwanja.engines import Resolved
from uwanja.fastchess import Fastchess
from uwanja.hostinfo import HostInfo
from uwanja.openings import fens_from_pgn, own_book
from uwanja.resume import ResumeError, known_engines, missing_pairs, play_block, segments
from uwanja.schedule import Schedule
from uwanja.session import RunPaths, Segment, Session

FENS = [f"rnbqkbnr/pppppppp/8/8/8/{n}P5/PPPPPPPP/RNBQKBNR w KQkq - 0 1".replace("{n}", str(n)) for n in range(1, 8)]


def game(round_, white, black, result="1-0", fen=None):
    tags = [("Event", "t"), ("Round", round_), ("White", white), ("Black", black), ("Result", result)]
    if fen:
        tags += [("SetUp", "1"), ("FEN", fen)]
    head = "\n".join(f'[{key} "{value}"]' for key, value in tags)
    return f"{head}\n\n1. e4 {{+0.1/1 0.1s, White mates}} {result}\n\n"


def make(tmp_path, rounds=3, opponents=("a", "b"), book=None):
    raw = {"name": "g", "tc": "1+0", "rounds": rounds, "subject": {"cmd": "s"},
           "opponent": [{"cmd": o} for o in opponents]}
    if book:
        raw["book"] = str(book)
    gauntlet = parse(raw)
    engines = [Resolved(spec, Path(sys.executable), Path("."), {"kind": "test"}, spec.id) for spec in gauntlet.engines]
    identity = Identity("m", gauntlet, engines[0], engines[1:], HostInfo("windows", "x86_64"), "x")
    paths = RunPaths(tmp_path / "run")
    paths.root.mkdir()
    return gauntlet, engines, identity, paths


class Reporter:
    def __init__(self): self.documents, self.games = [], []
    def start(self): pass
    def close(self): pass
    def put_match(self, document, final=False): self.documents.append(document)
    def put_game(self, number, pgn, final=False, moves=None): self.games.append((number, final))


def test_repeated_slots_count_once_the_first_wins(tmp_path):
    gauntlet, engines, identity, paths = make(tmp_path)
    paths.pgn.write_text(game("1", "s", "a", "1-0") + game("1", "a", "s", "1/2-1/2") + game("2", "s", "b", "0-1")
                         # the stopped run's games again, other results (an engine's luck), and the new ones
                         + game("1", "s", "a", "0-1") + game("2", "s", "b", "1-0") + game("2", "b", "s", "1-0"))
    session = Session(gauntlet, identity, paths, None, log=lambda text: None)
    assert session.restore() == 4
    assert session.repeats == 2
    rows = {row["opponent"]: row for row in session.results.to_json()}
    assert (rows["a"]["wins"], rows["a"]["draws"], rows["a"]["losses"]) == (1, 1, 0)
    assert (rows["b"]["wins"], rows["b"]["draws"], rows["b"]["losses"]) == (0, 0, 2)
    assert sorted(session._counted) == [1, 2, 3, 4]
    assert session.schedule.progress()["played"] == 4


def test_missing_pairs_are_per_opponent_and_round_major():
    # 2 opponents, 3 rounds: pair p is round p // 2 of opponent p % 2, its games are 2p+1 and 2p+2
    everything = set(range(1, 13))
    assert missing_pairs(everything, 2, 3) == [[], []]
    assert missing_pairs(everything - {4}, 2, 3) == [[], [1]]             # pair 1 has only its first game
    assert missing_pairs({1}, 2, 2) == [[0, 2], [1, 3]]


def test_openings_are_chosen_by_uwanja_and_pair_p_has_book_p_mod_rounds(tmp_path):
    book = tmp_path / "book.epd"
    book.write_text("\n".join(FENS) + "\n")
    gauntlet, *_ = make(tmp_path, rounds=4, book=book)
    out = tmp_path / "openings.epd"
    assert own_book(gauntlet, 5, out)
    first = out.read_text()
    assert own_book(gauntlet, 5, out) and out.read_text() == first        # the seed is the order
    lines = first.splitlines()
    assert len(lines) == 4 and set(lines) <= set(FENS)


def test_fens_from_pgn_reads_the_pair_from_round():
    games = [("1", "A"), ("6", "B"), ("3", None), ("x", "C")]
    assert fens_from_pgn(games, 4) == {0: "A", 1: "B"}


def test_segments_play_missing_pairs_on_their_own_openings(tmp_path):
    book = tmp_path / "book.epd"
    book.write_text("\n".join(FENS) + "\n")
    gauntlet, engines, identity, paths = make(tmp_path, rounds=3, book=book)
    (paths.root / "openings.epd").write_text("\n".join(FENS[:3]) + "\n")      # pair p has FENS[p % 3]
    plan = segments({1, 2, 3, 4, 5, 6}, gauntlet, Fastchess(Path("fastchess"), "1"), engines, paths)
    by = {s.opponent: s for s in plan}
    assert by["a"].pairs == [4] and by["b"].pairs == [3, 5]               # pairs 0..2 are done
    assert (paths.root / "resume-1-a.epd").read_text().splitlines() == [FENS[4 % 3]]
    assert (paths.root / "resume-1-b.epd").read_text().splitlines() == [FENS[3 % 3], FENS[5 % 3]]
    argv = by["b"].argv
    assert argv[argv.index("-rounds") + 1] == "2"
    assert "order=sequential" in argv
    assert [a for a in argv if a.startswith("name=")] == ["name=s", "name=b"]
    # a second resume doesn't overwrite the first one's files
    again = segments({1, 2, 3, 4, 5, 6}, gauntlet, Fastchess(Path("fastchess"), "1"), engines, paths)
    assert again[0].pgn.name == "resume-2-a.pgn"


def test_segment_maps_its_games_back_to_the_gauntlet():
    segment = Segment(["x"], Path("p"), Path("l"), Path("o"), pairs=[2, 4], opponent="a")
    assert [segment.number_of(g) for g in (1, 2, 3, 4)] == [5, 6, 9, 10]
    assert segment.entries() == [5, 6, 9, 10]
    text = '[Event "e"]\n[Round "2"]\n[White "s"]\n'
    assert segment.remap(text, "2") == ('[Event "e"]\n[Round "5"]\n[White "s"]\n', 5)
    assert segment.remap(text, "9")[1] is None


def test_bind_among_takes_finished_entries_as_games_played_again():
    schedule = Schedule("s", ["a"], 3)
    schedule.finished(schedule.get(3), "1-0", None)
    first = schedule.bind("s", "a", [3, 5], set())
    second = schedule.bind("s", "a", [3, 5], {first.number})
    assert (first.number, second.number) == (3, 5)


def test_needs_the_binaries_it_started_with(tmp_path):
    gauntlet, engines, identity, paths = make(tmp_path)
    runner = Fastchess(Path(sys.executable), "1")
    assert known_engines(gauntlet, identity) is None                      # a run from before the block
    identity.play = play_block(runner, engines, 1, False)
    assert known_engines(gauntlet, identity) is not None
    identity.play["engines"]["a"]["sha256"] = "0" * 64
    try:
        known_engines(gauntlet, identity)
    except ResumeError as error:
        assert "a:" in str(error) and "SHA-256" in str(error)
    else:
        raise AssertionError("a changed binary was accepted")
    identity.play["engines"]["a"]["cmd"] = str(tmp_path / "gone.exe")
    try:
        known_engines(gauntlet, identity)
    except ResumeError as error:
        assert "gone" in str(error)
    else:
        raise AssertionError("a missing binary was accepted")


FAKE_FASTCHESS = textwrap.dedent('''
    import sys
    pgn, out = sys.argv[1], sys.argv[2]
    games = [("1", "s", "a", "1-0"), ("1", "a", "s", "1/2-1/2"), ("2", "s", "a", "0-1"), ("2", "a", "s", "0-1")]
    for number, (round_, white, black, result) in enumerate(games, 1):
        print(f"Started game {number} of 4 ({white} vs {black})", flush=True)
    for number, (round_, white, black, result) in enumerate(games, 1):
        print(f"Finished game {number} ({white} vs {black}): {result} {{x}}", flush=True)
        with open(pgn, "a") as handle:
            handle.write(f'[Event "e"]\\n[Round "{round_}"]\\n[White "{white}"]\\n[Black "{black}"]\\n'
                         f'[Result "{result}"]\\n\\n1. e4 {{+0.1/1 0.1s, White mates}} {result}\\n\\n')
''')


def test_a_resumed_run_plays_its_segments_and_counts_every_slot_once(tmp_path):
    gauntlet, engines, identity, paths = make(tmp_path, rounds=3, opponents=("a",))
    # the first run got through pair 0 and one game of pair 1 (games 3), pair 2 never started
    paths.pgn.write_text(game("1", "s", "a", "1-0") + game("1", "a", "s", "1/2-1/2") + game("2", "s", "a", "1-0"))
    reporter = Reporter()
    session = Session(gauntlet, identity, paths, reporter, log=lambda text: None)
    assert session.restore() == 3
    plan = segments(session._counted, gauntlet, Fastchess(Path("fastchess"), "1"), engines, paths)
    assert [(s.opponent, s.pairs) for s in plan] == [("a", [1, 2])]
    script = tmp_path / "fake.py"
    script.write_text(FAKE_FASTCHESS)
    segment = plan[0]
    segment.argv = [sys.executable, str(script), str(segment.pgn), str(segment.out)]
    assert session.run(None, segments=plan) == 0
    rounds = [line for line in paths.pgn.read_text().splitlines() if line.startswith("[Round")]
    assert rounds == [f'[Round "{n}"]' for n in (1, 1, 2, 2, 2, 3, 3)]    # the new games under their own Round
    assert session.repeats == 1
    document = json.loads(paths.match.read_text())
    assert document["match"]["state"] == "finished"
    row = document["results"][0]
    assert row["games"] == 6                          # three rounds, two games each, none twice
    assert document["progress"] == {"total": 6, "played": 6, "playing": 0}
    # game 3 (pair 1, subject white) was done before and played again as 0-1: the first, 1-0, stays
    assert (row["wins"], row["draws"], row["losses"]) == (3, 2, 1)
    assert [entry["state"] for entry in document["games"]] == ["finished"] * 6
