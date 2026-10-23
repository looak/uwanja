"""A game in progress as PGN, the way fastchess writes its own: `1. e4 {+0.31/18 0.452s} e5 {-0.20/17 0.440s}`.

nyayo reads evals, depths and times from the comments, so these are fastchess' format and the live game and the
finished one (fastchess' own PGN, sent when the game ends) look the same. The result is `*` until then, whatever the
board says, because a game with a result is sealed on nyayo's side.
"""
from __future__ import annotations

import io
import time
from typing import Iterable

import chess
import chess.pgn

from .tracker import FinishedGame, Game, MoveRecord


# what fastchess' -pgnout can add to a move's comment, in the order it writes them (`tl` and `latency` come before
# these and aren't known from the log). A value the engine didn't report is written as 0, as fastchess does.
EXTRAS = ("nodes", "seldepth", "nps", "hashfull", "tbhits", "pv")


def comment(record: MoveRecord, fields: frozenset[str] | set[str] = frozenset()) -> str:
    """A move's comment as fastchess writes it: the mover's score and depth, the time, then the `-pgnout` extras,
    `+0.31/18 0.452s, n=2200412, nps=0, pv="g1f3 g8f6"`, `-M12/9 0.015s`, `book`."""
    if record.book:
        return "book"
    seconds = f"{(record.think_ms or 0) / 1000:.3f}s"
    cp, mate = record.cp, record.mate
    sign = 1 if record.white else -1       # records hold white's view, the comment is the mover's
    if mate is not None:
        moves = mate * sign                # the engine says moves; fastchess writes plies: 2n-1 to win, 2n to lose
        value = f"+M{2 * moves - 1}" if moves > 0 else f"-M{2 * abs(moves)}"
    elif cp is not None:
        value = f"{cp * sign / 100:+.2f}" if cp else "0.00"       # fastchess writes a zero without a sign
    else:
        value = ""                         # fastchess: `/0 0.000s`
    parts = [f"{value}/{record.depth or 0} {seconds}"]
    numbers = {"nodes": ("n", record.nodes), "seldepth": ("sd", record.seldepth), "nps": ("nps", record.nps),
               "hashfull": ("hashfull", record.hashfull), "tbhits": ("tbhits", record.tbhits)}
    for name in EXTRAS:
        if name not in fields:
            continue
        if name == "pv":
            parts.append('pv="' + " ".join(record.pv) + '"')
        else:
            label, number = numbers[name]
            parts.append(f"{label}={number or 0}")
    return ", ".join(parts)


def live_moves(game: Game, fields: frozenset[str] | set[str] = frozenset()) -> list[tuple[str, str]]:
    """Every move of the game so far as (SAN, comment), the comment without its braces: what a PATCH to nyayo
    carries, and what `live_pgn` writes."""
    return [(record.san, comment(record, fields)) for record in game.moves]


def live_pgn(game: Game, event: str, tc: str | None, names: dict[str, str] | None = None,
             round_: int | None = None, fields: frozenset[str] | set[str] = frozenset()) -> str:
    """The game so far, whole, `[Result "*"]`, written the way fastchess writes a PGN (python-chess would add
    spaces inside the braces and number every black move). `names` maps the match's engine names to their
    `id name`."""
    tags = [("Event", event), ("Site", "?"),
            ("Date", time.strftime("%Y.%m.%d", time.localtime(game.started_at))), ("Round", str(round_ if round_ is not None else game.number)),
            ("White", game.white or "?"), ("Black", game.black or "?"), ("Result", "*")]
    for tag, engine in (("EngineWhiteName", game.white), ("EngineBlackName", game.black)):
        if names and engine and engine in names:
            tags.append((tag, names[engine]))
    if tc:
        tags.append(("TimeControl", tc))
    if game.start_fen != chess.STARTING_FEN:
        tags += [("SetUp", "1"), ("FEN", game.start_fen)]
    head = "".join(f'[{key} "{_escape(value)}"]\n' for key, value in tags)

    board = chess.Board(game.start_fen)
    tokens: list[str] = []
    for index, record in enumerate(game.moves):
        if board.turn == chess.WHITE:
            tokens.append(f"{board.fullmove_number}.")
        elif index == 0:
            tokens.append(f"{board.fullmove_number}...")
        tokens.append(record.san)
        tokens.append("{" + comment(record, fields) + "}")
        board.push_uci(record.uci)
    tokens.append("*")
    return head + "\n" + _wrap(tokens) + "\n\n"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _wrap(tokens: list[str], width: int = 80) -> str:
    """Movetext in lines of at most `width` characters, a comment's words kept together as fastchess does."""
    lines, current = [], ""
    for token in tokens:
        candidate = f"{current} {token}" if current else token
        if len(candidate) > width and current:
            lines.append(current)
            current = token
        else:
            current = candidate
    if current:
        lines.append(current)
    return "\n".join(lines)


def read_games(text: str) -> Iterable[tuple[chess.pgn.Game, str]]:
    """(parsed game, its text) for every game in a PGN, each keeping the text it was written as."""
    for chunk in _split(text):
        game = chess.pgn.read_game(io.StringIO(chunk))
        if game is not None:
            yield game, chunk


def _split(text: str) -> list[str]:
    chunks, current = [], []
    for line in text.splitlines():
        if line.startswith("[Event ") and any(entry and not entry.startswith("[") for entry in current):
            chunks.append("\n".join(current).strip() + "\n\n")
            current = []
        current.append(line)
    if any(entry.strip() for entry in current):
        chunks.append("\n".join(current).strip() + "\n\n")
    return chunks


def to_finished(game: chess.pgn.Game, text: str) -> FinishedGame:
    headers = game.headers
    return FinishedGame(
        start_fen=headers.get("FEN", chess.STARTING_FEN), moves=[move.uci() for move in game.mainline_moves()],
        result=headers.get("Result", "*"), termination=headers.get("Termination"), white=headers.get("White"),
        black=headers.get("Black"), text=text, round=headers.get("Round"), comment=last_comment(game))


def last_comment(game: chess.pgn.Game) -> str:
    """The final move's comment, where fastchess says how a game ended: `White loses on time`, `Black mates`."""
    node = game.end()
    return node.comment if node is not game else ""
