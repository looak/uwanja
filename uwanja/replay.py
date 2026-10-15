"""Replays a recorded runner log into a new file at the pace it was written, a match to develop against.

    python -m uwanja replay games.log live.log [--pgn games.pgn live.pgn] [--speed 2] [--max-gap 5]

Works with fastchess logs and cutechess -debug output, the timestamps on the lines set the pace. With --pgn, each game
of the recorded PGN is appended to the new PGN right after its last move went by in the replayed log, as the runner
would have written it.
"""
from __future__ import annotations

import argparse
import io
import re
import sys
import time

import chess
import chess.pgn

from .events import EngineIO, parse_fastchess_line
from .uci import parse_position

# [Engine] [21:11:14.495571] <  3>  dev <--- go ...    [INFO  ] [21:11:14.495571] <   > fastchess --- ...
_FASTCHESS_TIME = re.compile(r"^\[[A-Za-z ]+\]\s*\[(\d+):(\d+):(\d+(?:\.\d+)?)\]")
# 1234 >dev(0): go ...
_CUTECHESS_TIME = re.compile(r"^(\d+) [<>]")


def line_time(line: str) -> float | None:
    """Seconds into the day (fastchess) or the run (cutechess), None for lines without a time."""
    match = _FASTCHESS_TIME.match(line)
    if match:
        hours, minutes, seconds = match.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    match = _CUTECHESS_TIME.match(line)
    if match:
        return int(match.group(1)) / 1000
    return None


def read_pgn_games(path: str) -> list[tuple[str, list[str], str]]:
    """(start fen, uci moves, the game's PGN text) for every game in path."""
    with open(path, encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    games = []
    # split on [Event so each game keeps its own text, python-chess reads the moves.
    for chunk in re.split(r"\n(?=\[Event )", text):
        if not chunk.strip():
            continue
        game = chess.pgn.read_game(io.StringIO(chunk))
        if game is None:
            continue
        fen = game.headers.get("FEN", chess.STARTING_FEN)
        games.append((fen, [move.uci() for move in game.mainline_moves()], chunk.strip() + "\n\n"))
    return games


def pgn_release_points(lines: list[str], games: list[tuple[str, list[str], str]]) -> dict[int, list[str]]:
    """Line index -> the PGN texts to write after that line: after the engine's bestmove that answered the game's
    last position, or at the end for games the log doesn't show."""
    events: list[EngineIO | None] = [parse_fastchess_line(line) for line in lines]
    last_position: dict[tuple[str, tuple[str, ...]], int] = {}
    for index, event in enumerate(events):
        if event is not None and event.text.startswith("position "):
            position = parse_position(event.text)
            if position is not None:
                key = (" ".join(position.fen.split()[:4]), tuple(position.moves))
                last_position[key] = index
    release: dict[int, list[str]] = {}
    for fen, moves, text in games:
        fen4 = " ".join(fen.split()[:4])
        found = [last_position.get((fen4, tuple(moves[:n]))) for n in (len(moves) - 1, len(moves))]
        found = [index for index in found if index is not None]
        at = len(lines) - 1
        if found:
            start = max(found)
            slot = events[start].slot
            for index in range(start + 1, len(lines)):
                event = events[index]
                if event is not None and event.slot == slot and event.text.startswith(("bestmove", "ucinewgame")):
                    at = index
                    break
        release.setdefault(at, []).append(text)
    return release


def replay(source: str, target: str, pgn: tuple[str, str] | None = None, speed: float = 1.0,
           max_gap: float = 5.0, out=sys.stderr) -> None:
    with open(source, encoding="utf-8", errors="replace") as handle:
        lines = handle.read().splitlines()
    release = pgn_release_points(lines, read_pgn_games(pgn[0])) if pgn else {}
    pgn_out = open(pgn[1], "w", encoding="utf-8") if pgn else None
    previous: float | None = None
    behind = 0.0            # recorded seconds replayed so far, idle stretches cut to max_gap
    started = time.monotonic()
    written = 0
    try:
        with open(target, "w", encoding="utf-8", newline="\n") as log_out:
            for index, line in enumerate(lines):
                stamp = line_time(line)
                if stamp is not None:
                    if previous is not None:
                        gap = stamp - previous
                        if gap < -43200:
                            gap += 86400     # past midnight
                        behind += min(max(gap, 0.0), max_gap)
                        # sleep against the wall clock, short sleeps on their own would add up to far too long.
                        wait = started + behind / speed - time.monotonic()
                        if wait > 0.02:
                            log_out.flush()
                            if pgn_out:
                                pgn_out.flush()
                            time.sleep(wait)
                    previous = stamp
                log_out.write(line + "\n")
                written += 1
                for text in release.get(index, ()):
                    pgn_out.write(text)
                if written % 5000 == 0:
                    print(f"{written}/{len(lines)} lines", file=out)
    finally:
        if pgn_out:
            pgn_out.close()
    print(f"done, {written} lines", file=out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m uwanja replay", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", help="a recorded fastchess log or cutechess -debug output")
    parser.add_argument("target", help="the file to write, the runner follows this one")
    parser.add_argument("--pgn", nargs=2, metavar=("SOURCE", "TARGET"), help="the recorded PGN and the one to write")
    parser.add_argument("--speed", type=float, default=1.0, help="faster than recorded, 2 is twice as fast")
    parser.add_argument("--max-gap", type=float, default=5.0, metavar="SECONDS",
                        help="longest pause between lines, idle stretches are cut to this (default 5)")
    args = parser.parse_args(argv)
    try:
        replay(args.source, args.target, tuple(args.pgn) if args.pgn else None, args.speed, args.max_gap)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
