"""Turns a stream of EngineIO events into games, move by move.

fastchess writes its PGN only when a game ends, the log has every move as it's made. A runner plays games in slots,
one after another per slot, several slots at once; the tracker keeps the current game of every slot: who plays white
and black, the moves with each engine's last `info` before it moved (score, depth, time), and, when the PGN has it,
the result. Ported from bwawa's tracker, without telemetry, SPRT and the viewer's summaries.
"""
from __future__ import annotations

import time as wallclock
from dataclasses import dataclass, field
from typing import Callable

import chess
import chess.pgn

from .events import FROM_ENGINE, TO_ENGINE, EngineIO
from .uci import Info, parse_bestmove, parse_info, parse_position


def white_view(info: Info, white_to_move: bool) -> tuple[int | None, int | None]:
    """(centipawns, mate in moves) from white's side, UCI scores are the side to move's."""
    sign = 1 if white_to_move else -1
    cp = info.score_cp * sign if info.score_cp is not None else None
    mate = info.mate * sign if info.mate is not None else None
    return cp, mate


@dataclass
class MoveRecord:
    ply: int
    uci: str
    san: str
    white: bool                     # white made the move
    book: bool = False              # came with the opening, no search
    cp: int | None = None           # white's view
    mate: int | None = None         # white's view
    depth: int | None = None
    seldepth: int | None = None
    nodes: int | None = None
    nps: int | None = None          # as the engine reported it, None when it didn't
    hashfull: int | None = None
    tbhits: int | None = None
    pv: list[str] = field(default_factory=list)      # UCI moves
    think_ms: int | None = None     # go to bestmove, measured by the runner's log


@dataclass
class LiveSearch:
    engine: str
    white: bool
    started: float
    info: Info | None = None


@dataclass
class FinishedGame:
    """A game as fastchess' PGN has it, the authority on how it ended."""
    start_fen: str
    moves: list[str]
    result: str
    termination: str | None
    white: str | None
    black: str | None
    text: str                                   # the game's PGN, as written
    round: str | None = None
    comment: str = ""                           # the final move's comment, how the game ended
    seen: float = field(default_factory=wallclock.monotonic)


class Game:
    def __init__(self, number: int, slot: str, start_fen: str, now: float):
        self.number = number            # the runner's game number in the match, nyayo's {game}
        self.slot = slot
        self.start_fen = start_fen
        self.board = chess.Board(start_fen)
        self.white: str | None = None
        self.black: str | None = None
        self.moves: list[MoveRecord] = []
        self.live: LiveSearch | None = None
        self.status = "live"            # live | finished | abandoned
        self.result: str | None = None
        self.started_at = wallclock.time()

    def uci_moves(self) -> list[str]:
        return [move.uci for move in self.moves]

    def push(self, uci: str, book: bool) -> MoveRecord | None:
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            return None
        if move not in self.board.legal_moves:
            return None
        record = MoveRecord(ply=len(self.moves) + 1, uci=uci, san=self.board.san(move),
                            white=self.board.turn == chess.WHITE, book=book)
        self.board.push(move)
        self.moves.append(record)
        return record


class Tracker:
    """Keeps the current game of every slot.

    `on_update(game)` is called whenever a game's moves changed (a game started, a move was made). How a game ended
    is not the tracker's business: the log doesn't always show the last move (see below), the PGN is the authority
    and the session reads it on its own.

    Known limit: fastchess logs an engine's output under the thread that read it, which for some engines (Leorik,
    Weiawaga, Lynx, ...) is not the thread that wrote its commands, so its `info` and `bestmove` lines don't find
    their game. The move still arrives with the next `position`, only without its eval, and a game's last move is
    only in the PGN."""

    def __init__(self, on_update: Callable[[Game], None] | None = None):
        self.on_update = on_update or (lambda game: None)
        self.slots: dict[str, Game] = {}
        self.games: dict[int, Game] = {}
        self.pending_new: set[str] = set()
        self.engine_ids: dict[str, str] = {}            # engine name in the match -> its `id name`
        self._numbers = 0

    def next_number(self) -> int:
        self._numbers += 1
        return self._numbers

    # --- log events ---
    def handle(self, event: EngineIO) -> None:
        if event.direction == TO_ENGINE:
            self._to_engine(event)
        else:
            self._from_engine(event)

    def _to_engine(self, event: EngineIO) -> None:
        text = event.text
        if text == "ucinewgame":
            self.pending_new.add(event.slot)
        elif text.startswith("position "):
            position = parse_position(text)
            if position is not None:
                self._sync_position(event, position.fen, position.moves)
                self._learn_name(event)
        elif text.startswith("go"):
            game = self.slots.get(event.slot)
            if game is None or game.status != "live":
                return
            white = game.board.turn == chess.WHITE
            if white:
                game.white = game.white or event.engine
            else:
                game.black = game.black or event.engine
            game.live = LiveSearch(event.engine, white, event.time)

    def _learn_name(self, event: EngineIO) -> None:
        """The runner sends both engines of a game their positions, once one side is known the other engine is the
        opponent, before it's asked to move."""
        game = self.slots.get(event.slot)
        if game is None or game.status != "live":
            return
        if game.white and not game.black and event.engine != game.white:
            game.black = event.engine
        elif game.black and not game.white and event.engine != game.black:
            game.white = event.engine

    def _from_engine(self, event: EngineIO) -> None:
        text = event.text
        if "id name " in text and (text.startswith("id name ") or text.lstrip("> ").startswith("id name ")):
            self.engine_ids[event.engine] = text.split("id name ", 1)[1].strip()
            return
        game = self.slots.get(event.slot)
        if game is None or game.live is None or game.live.engine != event.engine:
            return
        if text.startswith("info"):
            info = parse_info(text)
            if info is not None:
                game.live.info = info
        elif text.startswith("bestmove"):
            uci = parse_bestmove(text)
            live, game.live = game.live, None
            if uci is None:
                return
            record = game.push(uci, book=False)
            if record is None:
                return
            if live.info is not None:
                record.cp, record.mate = white_view(live.info, live.white)
                info = live.info
                record.depth, record.seldepth, record.nodes = info.depth, info.seldepth, info.nodes
                record.nps, record.hashfull, record.tbhits = info.nps_reported, info.hashfull, info.tbhits
                record.pv = list(info.pv)
            record.think_ms = int((event.time - live.started) * 1000)
            self.on_update(game)

    def _sync_position(self, event: EngineIO, fen: str, moves: list[str]) -> None:
        game = self.slots.get(event.slot)
        new_game = (game is None or game.start_fen.split()[:4] != fen.split()[:4]
                    or moves[:len(game.moves)] != game.uci_moves())
        if not new_game and event.slot in self.pending_new and len(moves) < len(game.moves):
            new_game = True
        changed = new_game
        if new_game:
            if game is not None and game.status == "live":
                game.status = "abandoned"   # ended in a way the stream doesn't show: adjudication, time, a crash
                game.live = None
            game = Game(self.next_number(), event.slot, fen, event.time)
            self.slots[event.slot] = game
            self.games[game.number] = game
        self.pending_new.discard(event.slot)
        # moves this game hasn't seen: the opening before anyone searched, later a move the log missed.
        book = all(record.book for record in game.moves)
        for uci in moves[len(game.moves):]:
            if game.push(uci, book=book) is None:
                break
            changed = True
        if changed:
            self.on_update(game)
