"""Continuing a stopped gauntlet as the same match.

fastchess can resume from its saved config, but not a gauntlet of more than two engines (it drops the saved stats
then, v1.8.2-alpha) and not exactly when games finish out of order. So uwanja does it from the games themselves: the
first finished game of every slot (round, colours) in games.pgn counts, and for each opponent the pairs of games that
have no complete result get a fastchess of their own, on the openings they would have had. See `docs/spec.md`.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

from . import fastchess as fastchess_mod
from .config import ConfigError, Gauntlet
from .document import Identity, sha256
from .engines import Resolved
from .fastchess import Fastchess
from .openings import book_lines, fen_of, fens_from_pgn
from .pgnout import read_games
from .session import RunPaths, Segment, Session


class ResumeError(ConfigError):
    """The run can't be continued, said in a sentence."""


def play_block(runner: Fastchess, engines: list[Resolved], book_seed: int, owned_book: bool) -> dict:
    """What identity.json keeps so the run can be started again without fetching or probing: fastchess, each engine's
    executable with its SHA-256, and how the openings were chosen."""
    return {"fastchess": {"exe": str(runner.exe), "version": runner.version, "sha256": sha256(runner.exe)},
            "engines": {engine.spec.id: {"cmd": str(engine.cmd), "dir": str(engine.dir),
                                         "sha256": sha256(engine.cmd)} for engine in engines},
            "book_seed": book_seed, "openings": owned_book}


def known_engines(gauntlet: Gauntlet, identity: Identity) -> tuple[Fastchess, list[Resolved]] | None:
    """fastchess and the engines as the run started them, checked: every file is there and is the one hashed. None
    for a run from before they were kept."""
    play = identity.play
    if not play:
        return None
    runner = Fastchess(Path(play["fastchess"]["exe"]), play["fastchess"]["version"])
    checks = [("fastchess", runner.exe, play["fastchess"].get("sha256"))]
    engines = []
    for spec in gauntlet.engines:
        kept = play["engines"].get(spec.id)
        if kept is None:
            raise ResumeError(f"identity.json has no executable for {spec.id}")
        engines.append(Resolved(spec, Path(kept["cmd"]), Path(kept["dir"]), {"kind": "resume"}, spec.name or spec.id))
        checks.append((spec.id, engines[-1].cmd, kept.get("sha256")))
    problems = []
    for name, path, expected in checks:
        if not path.is_file():
            problems.append(f"{name}: {path} is gone")
        elif expected and sha256(path) != expected:
            problems.append(f"{name}: {path} is not the file the run started with (SHA-256 differs)")
    if problems:
        raise ResumeError("the run can't be continued with other binaries:\n  " + "\n  ".join(problems))
    return runner, engines


def missing_pairs(counted: set[int], opponents: int, rounds: int) -> list[list[int]]:
    """Per opponent, the pairs (0-based, round-major: `round * opponents + opponent`) that don't have both games."""
    return [[pair for pair in (r * opponents + j for r in range(rounds))
             if not {2 * pair + 1, 2 * pair + 2} <= counted] for j in range(opponents)]


def openings_for(paths: RunPaths, gauntlet: Gauntlet) -> dict[int, str] | None:
    """Opening index -> its line in the book, from the run's own openings.epd, or else from the FEN of the games
    already played; None for a run without a book."""
    if not gauntlet.book:
        return None
    own = paths.root / "openings.epd"
    if own.exists():
        return dict(enumerate(book_lines(own)))
    text = paths.pgn.read_text(encoding="utf-8", errors="replace") if paths.pgn.exists() else ""
    return fens_from_pgn([(game.headers.get("Round"), fen_of(chunk)) for game, chunk in read_games(text)],
                         gauntlet.rounds)


def segments(counted: set[int], gauntlet: Gauntlet, runner: Fastchess, engines: list[Resolved],
             paths: RunPaths) -> list[Segment]:
    """One fastchess per opponent with pairs left, in the opponent's order, each playing its missing pairs on the
    openings they have in the gauntlet. Both games of a pair are played, a pair with one game done plays that one
    again and the repeat is ignored."""
    opponents = engines[1:]
    missing = missing_pairs(counted, len(opponents), gauntlet.rounds)
    book = openings_for(paths, gauntlet)
    number = 1
    while any(paths.root.glob(f"resume-{number}-*")):
        number += 1
    found = []
    for opponent, pairs in zip(opponents, missing):
        if not pairs:
            continue
        stem = f"resume-{number}-{opponent.spec.id}"
        changes: dict = {"rounds": len(pairs)}
        if book is not None:
            lines = []
            for pair in pairs:
                line = book.get(pair % gauntlet.rounds)
                if line is None:
                    raise ResumeError(
                        f"the opening of pair {pair + 1} ({opponent.spec.id}) isn't known: this run didn't keep its "
                        f"openings and no finished game started from it yet; start the gauntlet again")
                lines.append(line)
            sub = paths.root / f"{stem}.epd"
            sub.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
            changes |= {"book": str(sub), "book_order": "sequential", "book_format": "epd"}
        smaller = dataclasses.replace(gauntlet, **changes)
        pgn, log, out = paths.root / f"{stem}.pgn", paths.root / f"{stem}.log", paths.root / f"{stem}.out"
        argv = fastchess_mod.command(smaller, runner, [engines[0], opponent], pgn, log, paths.root / f"{stem}.json")
        found.append(Segment(argv, pgn, log, out, pairs, opponent.spec.id))
    return found
