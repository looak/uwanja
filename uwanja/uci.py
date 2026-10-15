"""Parsing of the UCI commands and replies the tracker cares about."""
from __future__ import annotations

from dataclasses import dataclass, field

import chess

START_FEN = chess.STARTING_FEN


@dataclass
class Position:
    fen: str
    moves: list[str]


def parse_position(text: str) -> Position | None:
    """position startpos|fen <fen> [moves ...]"""
    tokens = text.split()
    if len(tokens) < 2 or tokens[0] != "position":
        return None
    moves: list[str] = []
    if "moves" in tokens:
        index = tokens.index("moves")
        moves = tokens[index + 1:]
        tokens = tokens[:index]
    if tokens[1] == "startpos":
        return Position(START_FEN, moves)
    if tokens[1] == "fen" and len(tokens) >= 3:
        return Position(" ".join(tokens[2:]), moves)
    return None


def parse_go(text: str) -> dict[str, int]:
    """The numeric go parameters, wtime btime winc binc movestogo movetime depth nodes."""
    tokens = text.split()
    values: dict[str, int] = {}
    for key, value in zip(tokens[1:], tokens[2:]):
        if value.lstrip("-").isdigit():
            values[key] = int(value)
    return values


@dataclass
class Info:
    depth: int | None = None
    seldepth: int | None = None
    score_cp: int | None = None     # side to move's view
    mate: int | None = None         # side to move's view, moves
    nodes: int | None = None
    time_ms: int | None = None
    nps: int | None = None
    hashfull: int | None = None
    tbhits: int | None = None
    nps_reported: int | None = None     # what the engine said, nps above is worked out when it didn't
    pv: list[str] = field(default_factory=list)


_INT_FIELDS = {"depth", "seldepth", "nodes", "time", "nps", "hashfull", "multipv", "currmovenumber", "tbhits"}


def parse_info(text: str) -> Info | None:
    """An info line with a score, None for info strings, currmove updates and the like."""
    tokens = text.split()
    if not tokens or tokens[0] != "info" or "score" not in tokens:
        return None
    info = Info()
    i = 1
    while i < len(tokens):
        key = tokens[i]
        if key == "pv":
            info.pv = tokens[i + 1:]
            break
        if key == "score" and i + 2 < len(tokens):
            kind, value = tokens[i + 1], tokens[i + 2]
            if kind == "cp":
                info.score_cp = int(value)
            elif kind == "mate":
                info.mate = int(value)
            i += 3
            # lowerbound / upperbound
            while i < len(tokens) and tokens[i] in ("lowerbound", "upperbound"):
                i += 1
            continue
        if key in _INT_FIELDS and i + 1 < len(tokens):
            try:
                value = int(tokens[i + 1])
            except ValueError:
                value = None
            if key == "time":
                info.time_ms = value
            elif key in ("depth", "seldepth", "nodes", "nps", "hashfull", "tbhits"):
                setattr(info, key, value)
                if key == "nps":
                    info.nps_reported = value
            i += 2
            continue
        i += 1
    if info.nps is None and info.nodes and info.time_ms:
        info.nps = info.nodes * 1000 // max(info.time_ms, 1)
    return info


def parse_bestmove(text: str) -> str | None:
    tokens = text.split()
    if len(tokens) >= 2 and tokens[0] == "bestmove":
        return tokens[1]
    return None
