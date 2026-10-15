"""Every game of the gauntlet, known before the first move.

fastchess' gauntlet plays rounds; in a round each opponent gets a pair of games on one opening, the subject with
white first, then with black. So game N of `rounds * 2 * opponents` is a function of the config, and nyayo is told the
whole list at the start: played, playing and to be played.

fastchess names the game it's starting (`Started game 3 of 8 (subj vs opp-b)`) and how it ended (`Finished game 3
(subj vs opp-b): 1-0 {White mates}`); those lines are the authority and correct the schedule where it guessed
wrong. The log's games are matched to entries by who plays white, in the order fastchess starts them.
"""
from __future__ import annotations

import re

from .results import fault_of

STARTED = re.compile(r"^Started game (\d+) of (\d+) \((.+?) vs (.+?)\)\s*$")
FINISHED = re.compile(r"^Finished game (\d+) \((.+?) vs (.+?)\): (\S+)(?: \{(.*)\})?\s*$")


class Entry:
    def __init__(self, number: int, opponent: str, white: str, black: str):
        self.number = number
        self.round = (number + 1) // 2        # fastchess' PGN Round: a pair shares one
        self.opponent = opponent
        self.white, self.black = white, black
        self.state = "scheduled"               # scheduled | playing | finished
        self.result: str | None = None
        self.termination: str | None = None
        self.fault: dict | None = None
        self.bound = False                     # a game in the log has been matched to this entry

    def to_json(self, subject: str) -> dict:
        return {"number": self.number, "round": self.round, "opponent": self.opponent,
                "subject_colour": "white" if self.white == subject else "black", "state": self.state,
                "result": self.result, "termination": self.termination, "fault": self.fault}


class Schedule:
    def __init__(self, subject: str, opponents: list[str], rounds: int):
        self.subject = subject
        self.entries: list[Entry] = []
        for _ in range(rounds):
            for opponent in opponents:
                for subject_white in (True, False):
                    white, black = (subject, opponent) if subject_white else (opponent, subject)
                    self.entries.append(Entry(len(self.entries) + 1, opponent, white, black))

    def get(self, number: int) -> Entry | None:
        return self.entries[number - 1] if 1 <= number <= len(self.entries) else None

    def bind(self, white: str | None, black: str | None) -> Entry:
        """The entry for a game seen in the log: the first not yet matched where these two play these colours. A
        game the schedule has no place for (a recorded gauntlet with more rounds) is appended."""
        for entry in self.entries:
            if not entry.bound and (entry.white, entry.black) == (white, black):
                entry.bound = True
                return entry
        opponent = black if white == self.subject else white
        entry = Entry(len(self.entries) + 1, opponent or "?", white or "?", black or "?")
        entry.bound = True
        self.entries.append(entry)
        return entry

    def started(self, number: int, white: str, black: str) -> bool:
        entry = self.get(number)
        if entry is None:
            return False
        if (entry.white, entry.black) != (white, black):      # fastchess' word over the schedule's guess
            entry.white, entry.black = white, black
            entry.opponent = black if white == self.subject else white
        if entry.state == "scheduled":
            entry.state = "playing"
        return True

    def finished(self, entry: Entry, result: str, termination: str | None) -> None:
        entry.state, entry.result, entry.termination = "finished", result, termination or entry.termination
        found = fault_of(None, termination, result)
        if found is not None:
            kind, white_at_fault = found
            at_fault = entry.white if white_at_fault else entry.black
            entry.fault = {"kind": kind, "side": "subject" if at_fault == self.subject else "opponent"}

    def progress(self) -> dict:
        states = [entry.state for entry in self.entries]
        return {"total": len(states), "played": states.count("finished"), "playing": states.count("playing")}

    def to_json(self) -> list[dict]:
        return [entry.to_json(self.subject) for entry in self.entries]
