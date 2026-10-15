"""Counts per opponent from the subject's side, out of finished games' PGN headers and final comments.

The runner sends counts and computes no statistics, that's nyayo's. What it does count is what only the PGN carries:
the pentanomial (a game pair is two games of one round against one opponent, colours swapped) and, per side, time
losses, crashes and illegal moves.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# how a game ended, from the Termination tag and the final comment; fastchess words it `White loses on time`,
# `Black makes an illegal move: e2e5`, `White disconnects`, `White's connection stalls`.
_KINDS = (
    ("timelosses", re.compile(r"on time|time forfeit", re.IGNORECASE)),
    ("illegal", re.compile(r"illegal", re.IGNORECASE)),
    ("crashes", re.compile(r"disconnect|stall|crash|connection|abandon", re.IGNORECASE)),
)
_SIDE = re.compile(r"\b(White|Black)\b")
POINTS = {"1-0": (1.0, 0.0), "0-1": (0.0, 1.0), "1/2-1/2": (0.5, 0.5)}


def classify(termination: str | None, comment: str | None) -> str | None:
    """timelosses | illegal | crashes, or None for a game that ended the usual ways."""
    for text in (comment, termination):
        if text:
            for kind, pattern in _KINDS:
                if pattern.search(text):
                    return kind
    return None


def fault_of(termination: str | None, comment: str | None, result: str) -> tuple[str, bool] | None:
    """(kind, white was at fault) for a game that ended in a time loss, an illegal move or a crash, else None. The
    side is named in fastchess' words (`White loses on time`), the loser otherwise: it's the loser in all three."""
    kind = classify(termination, comment)
    if kind is None:
        return None
    side = _SIDE.search(comment or "") or _SIDE.search(termination or "")
    white = side[1] == "White" if side is not None else result == "0-1"
    return kind, white


@dataclass
class Row:
    opponent: str
    wins: int = 0
    draws: int = 0
    losses: int = 0
    penta: list[int] = field(default_factory=lambda: [0, 0, 0, 0, 0])
    paired: bool = True                     # False once a game came without a round to pair it by
    faults: dict[str, dict[str, int]] = field(default_factory=lambda: {
        kind: {"subject": 0, "opponent": 0} for kind in ("timelosses", "crashes", "illegal")})

    @property
    def games(self) -> int:
        return self.wins + self.draws + self.losses

    def to_json(self) -> dict:
        return {"opponent": self.opponent, "games": self.games, "wins": self.wins, "draws": self.draws,
                "losses": self.losses, "penta": list(self.penta) if self.paired else None,
                "timelosses": dict(self.faults["timelosses"]), "crashes": dict(self.faults["crashes"]),
                "illegal": dict(self.faults["illegal"])}


class Results:
    """Feed it finished games, ask it for the rows."""

    def __init__(self, subject: str, opponents: list[str]):
        self.subject = subject
        self.rows = {name: Row(name) for name in opponents}
        self._open: dict[tuple[str, str], float] = {}     # (round, opponent) -> the first game's points
        self.skipped = 0

    def add(self, white: str | None, black: str | None, result: str, termination: str | None = None,
            comment: str | None = None, round_: str | None = None) -> bool:
        """One finished game. False when it isn't the subject against a known opponent, or has no result yet."""
        if result not in POINTS or white is None or black is None:
            self.skipped += 1
            return False
        if white == self.subject:
            opponent, mine, theirs = black, POINTS[result][0], POINTS[result][1]
            subject_white = True
        elif black == self.subject:
            opponent, mine, theirs = white, POINTS[result][1], POINTS[result][0]
            subject_white = False
        else:
            self.skipped += 1
            return False
        row = self.rows.get(opponent)
        if row is None:
            self.skipped += 1
            return False
        if mine > theirs:
            row.wins += 1
        elif mine < theirs:
            row.losses += 1
        else:
            row.draws += 1

        found = fault_of(termination, comment, result)
        if found is not None:
            kind, at_fault_white = found
            row.faults[kind]["subject" if at_fault_white == subject_white else "opponent"] += 1

        if round_ is None:
            row.paired = False
        else:
            key = (round_, opponent)
            if key in self._open:
                row.penta[int((self._open.pop(key) + mine) * 2)] += 1
            else:
                self._open[key] = mine
        return True

    def totals(self) -> dict[str, int]:
        return {"games": sum(row.games for row in self.rows.values()),
                "wins": sum(row.wins for row in self.rows.values()),
                "draws": sum(row.draws for row in self.rows.values()),
                "losses": sum(row.losses for row in self.rows.values())}

    def to_json(self) -> list[dict]:
        return [row.to_json() for row in self.rows.values()]
