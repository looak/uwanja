"""Rebuilds a run directory's games and match document from fastchess' own PGN.

For a run whose schedule came out wrong (the October CCRL gauntlet: results given a new number when the log was slow,
37 phantom games past the end), or a gauntlet that was never run by uwanja. Every game is placed by its PGN `Round`
and colours, the first finished game of a slot counts, nothing is fetched or sent. The previous `games/` and
`match.json` are kept beside the new ones.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import load
from .document import Identity
from .pgnout import read_games, to_finished
from .results import Results
from .schedule import Schedule
from .session import RunPaths, reason_of, write_atomic


@dataclass
class Rebuilt:
    games: int
    entries: int
    duplicates: int
    document: dict


def rebuild(run_dir: str | Path) -> Rebuilt:
    paths = RunPaths(Path(run_dir))
    identity = Identity.from_json(json.loads(paths.identity.read_text(encoding="utf-8")))
    gauntlet = load(paths.config)
    old = json.loads(paths.match.read_text(encoding="utf-8")) if paths.match.exists() else None
    schedule = Schedule(identity.subject_id, [opponent["id"] for opponent in identity.opponents], gauntlet.rounds)
    results = Results(identity.subject_id, [opponent["id"] for opponent in identity.opponents])

    if paths.games.exists():
        backup = paths.root / "games.before-rebuild"
        if backup.exists():
            shutil.rmtree(backup)
        paths.games.rename(backup)
    if paths.match.exists():
        shutil.copy2(paths.match, paths.root / "match.before-rebuild.json")
    paths.games.mkdir()

    duplicates = placed = 0
    for game, text in read_games(paths.pgn.read_text(encoding="utf-8", errors="replace")):
        found = to_finished(game, text)
        entry = schedule.locate(found.round, found.white, found.black)
        if entry.state == "finished":
            duplicates += 1               # a slot played twice (a resume), the first game stands
            continue
        reason = reason_of(found.comment) or (found.termination if found.termination != "normal" else None)
        schedule.finished(entry, found.result, reason)
        results.add(found.white, found.black, found.result, found.termination, found.comment, found.round)
        write_atomic(paths.games / f"{entry.number}.pgn", found.text)
        placed += 1

    state = old["match"]["state"] if old else "stopped"
    finished_at = old["match"].get("finished_at") if old else None
    document = identity.document(results, "stopped" if state == "running" else state, finished_at, schedule)
    write_atomic(paths.match, json.dumps(document, indent=2) + "\n")
    return Rebuilt(placed, len(schedule.entries), duplicates, document)
