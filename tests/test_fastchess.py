from pathlib import Path

from uwanja.config import parse
from uwanja.engines import Resolved
from uwanja.fastchess import Fastchess, book_format, command
from uwanja.results import Results, classify
from uwanja.session import FileFollower, PgnFollower


def test_command_line():
    gauntlet = parse({
        "tc": "120+1", "book": "books/8moves.pgn", "rounds": 100, "concurrency": 14,
        "adjudication": {"win": "movecount=3 score=400", "draw": "movenumber=40 movecount=8 score=10"},
        "fastchess": {"args": ["-srand", "7"]},
        "subject": {"cmd": "elephant", "options": {"Threads": 1, "Hash": 64, "Ponder": False, "Move Overhead": 30}},
        "opponent": [{"cmd": "barbarossa", "args": ["--uci"], "fastchess_args": ["restart=on"]}]})
    engines = [Resolved(spec, Path(f"/e/{spec.id}"), Path("/e"), {}) for spec in gauntlet.engines]
    argv = command(gauntlet, Fastchess(Path("/fc"), "1"), engines, Path("g.pgn"), Path("g.log"), Path("c.json"))

    assert argv[0] == str(Path("/fc"))
    first, second = argv.index("-engine"), argv.index("-engine", argv.index("-engine") + 1)
    assert argv[first + 1].startswith("cmd=") and "name=elephant" in argv[first:second]
    assert {"option.Threads=1", "option.Hash=64", "option.Ponder=false", "option.Move Overhead=30"} <= set(argv[first:second])
    assert "args=--uci" in argv[second:] and "restart=on" in argv[second:]
    assert argv[argv.index("-tournament") + 1] == "gauntlet"
    assert argv[argv.index("-rounds") + 1] == "100" and "-repeat" in argv
    assert argv[argv.index("-concurrency") + 1] == "14"
    assert "format=pgn" in argv and "order=random" in argv
    assert argv[argv.index("-resign") + 1:argv.index("-resign") + 3] == ["movecount=3", "score=400"]
    assert argv[argv.index("-draw") + 1] == "movenumber=40"
    assert argv[-2:] == ["-srand", "7"]
    assert argv.index("-engine") < argv.index("-each")      # the subject first, fastchess' gauntlet seed


def test_book_format_from_extension():
    assert book_format(parse({"tc": "1+1", "book": "a.EPD", "subject": {"cmd": "a"}, "opponent": [{"cmd": "b"}]})) == "epd"


def test_classify_how_games_ended():
    assert classify("normal", "White loses on time") == "timelosses"
    assert classify("time forfeit", None) == "timelosses"
    assert classify("normal", "Black makes an illegal move: e2e5") == "illegal"
    assert classify("normal", "White disconnects") == "crashes"
    assert classify("normal", "+M3/9 0.1s, Black mates") is None
    assert classify("adjudication", "White wins by adjudication") is None


def test_results_from_the_subjects_side_with_faults():
    results = Results("elephant", ["bar"])
    # pair 1: win with white, then lose with black on time (the subject's fault)
    assert results.add("elephant", "bar", "1-0", "normal", "", "1")
    assert results.add("bar", "elephant", "1-0", "normal", "Black loses on time", "1")
    # pair 2: draw, then a win after the opponent's illegal move
    results.add("bar", "elephant", "1/2-1/2", "normal", "", "2")
    results.add("elephant", "bar", "1-0", "normal", "Black makes an illegal move: a1a2", "2")
    assert not results.add("elephant", "other", "1-0")       # not a known opponent
    assert not results.add("elephant", "bar", "*")           # unfinished
    row = results.to_json()[0]
    assert (row["wins"], row["draws"], row["losses"], row["games"]) == (2, 1, 1, 4)
    assert row["penta"] == [0, 0, 1, 1, 0] or row["penta"] == [0, 0, 1, 0, 1] or sum(row["penta"]) == 2
    assert row["penta"][2] == 1 and row["penta"][3] == 1       # 1+0 = 1 point, 0.5+1 = 1.5 points
    assert row["timelosses"] == {"subject": 1, "opponent": 0}
    assert row["illegal"] == {"subject": 0, "opponent": 1}


def test_follower_reads_what_grows(tmp_path):
    path = tmp_path / "x.log"
    follower = FileFollower(path)
    assert follower.read() == []
    path.write_bytes(b"one\ntwo\npar")
    assert follower.read() == ["one", "two"]
    with open(path, "ab") as handle:
        handle.write(b"tial\n")
    assert follower.read() == ["partial"]
    path.write_bytes(b"fresh\n")                  # started over
    assert follower.read() == ["fresh"]


def test_pgn_follower_releases_a_game_when_the_next_starts_or_the_file_is_quiet(tmp_path):
    path = tmp_path / "g.pgn"
    game = '[Event "e"]\n[Result "1-0"]\n\n1. e4 e5 1-0\n\n'
    path.write_text(game + game.replace('"e"', '"f"'), encoding="utf-8")
    follower = PgnFollower(path)
    assert len(follower.read()) == 1          # the first is complete, the second may still be growing
    assert len(follower.read()) == 1          # quiet: it's complete now
    assert follower.read() == []
