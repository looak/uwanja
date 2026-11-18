"""Which opening every pair of games is played from, known to uwanja so a stopped gauntlet can play only what's left.

fastchess' gauntlet takes the book's first `rounds` openings (after shuffling, for `order=random`) and hands them out
to pairs in the order it starts them, wrapping round: pair `p` (0-based, round-major: `(round - 1) * opponents +
opponent`) is `book[p % rounds]`. Its shuffle can't be reproduced outside fastchess, so for an EPD book uwanja makes
that list itself, writes it to the run directory as `openings.epd` and gives fastchess that, in order. A run
without it (a PGN book, a run from before this) has the openings its finished games started from, in their PGN.
"""
from __future__ import annotations

import random
import re
from pathlib import Path

from .config import Gauntlet


def book_lines(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip() and not line.startswith("#")]


def owns(gauntlet: Gauntlet) -> bool:
    """True for an EPD book that's there: uwanja chooses its openings."""
    from .fastchess import book_format
    source = gauntlet.path(gauntlet.book)
    return book_format(gauntlet) == "epd" and source is not None and source.is_file()


def own_book(gauntlet: Gauntlet, seed: int, dest: Path) -> bool:
    """Writes the `rounds` openings the gauntlet will play to `dest`. False when the book isn't one `owns` (or is
    shorter than the rounds), and fastchess keeps choosing."""
    if not owns(gauntlet):
        return False
    lines = book_lines(gauntlet.path(gauntlet.book))
    if len(lines) < gauntlet.rounds:
        return False
    if gauntlet.book_order == "random":
        random.Random(seed).shuffle(lines)
    dest.write_text("\n".join(lines[:gauntlet.rounds]) + "\n", encoding="utf-8", newline="\n")
    return True


def fens_from_pgn(games: list[tuple[str | None, str | None]], rounds: int) -> dict[int, str]:
    """Opening index -> FEN, from finished games `(Round, FEN)`: fastchess' `Round` is the pair, 1-based, so the pair
    `Round - 1` started from `book[(Round - 1) % rounds]`."""
    found: dict[int, str] = {}
    for round_, fen in games:
        if fen and round_ and round_.isdigit():
            found.setdefault((int(round_) - 1) % rounds, fen)
    return found


_FEN = re.compile(r'^\[FEN "([^"]*)"\]', re.MULTILINE)


def fen_of(text: str) -> str | None:
    found = _FEN.search(text)
    return found[1] if found else None
