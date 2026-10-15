"""The match document, schema v1 (ramani plans/match-schema.md, nyayo's once it lands).

Built whole every time: the producer sends the state it worked out from its own source and the counts so far, nyayo
keeps the latest.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import Gauntlet
from .engines import Resolved
from .hostinfo import HostInfo
from .results import Results
from .schedule import Schedule

SCHEMA_VERSION = 1
STATES = ("running", "finished", "stopped", "error")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def engine_json(engine: Resolved, with_id: bool) -> dict:
    spec = engine.spec
    data = {
        "name": engine.name or spec.id, "version": engine.version, "commit": engine.commit,
        "bench": engine.bench_nodes, "options": spec.options,
    }
    if with_id:
        data = {"id": spec.id, **data, "rating": spec.rating.to_json() if spec.rating else None}
    else:
        data["branch"] = engine.branch
        data["repo"] = engine.origin.get("url") if engine.origin.get("kind") == "release" else None
    return data


class Identity:
    """What stays fixed over a match: everything but the counts, the state and the timestamps."""

    def __init__(self, match_id: str, gauntlet: Gauntlet, subject: Resolved, opponents: list[Resolved],
                 host: HostInfo, runner_version: str, started_at: str | None = None):
        self.match_id = match_id
        self.started_at = started_at or now_iso()
        book = gauntlet.path(gauntlet.book)
        self.settings = {
            "tc": gauntlet.tc, "book": Path(gauntlet.book).name if gauntlet.book else None, "book_sha256": sha256(book),
            "runner": {"name": "fastchess", "version": runner_version},
            "concurrency": gauntlet.concurrency, "rounds": gauntlet.rounds,
            "adjudication": dict(gauntlet.adjudication) or None, "syzygy": False,
        }
        self.name = gauntlet.name
        self.host = host.describe()
        self.subject = engine_json(subject, with_id=False)
        self.opponents = [engine_json(engine, with_id=True) for engine in opponents]
        self.subject_id = subject.spec.id

    def document(self, results: Results, state: str, finished_at: str | None = None,
                 schedule: Schedule | None = None) -> dict:
        assert state in STATES
        return {
            "schema_version": SCHEMA_VERSION,
            "match": {
                "id": self.match_id, "kind": "gauntlet", "state": state,
                "producer": {"name": "uwanja", "version": __version__, "url": None},
                "started_at": self.started_at, "updated_at": now_iso(),
                "finished_at": finished_at if state != "running" else None,
                "host": self.host, "settings": self.settings, "name": self.name,
            },
            "subject": self.subject,
            "opponents": self.opponents,
            "results": results.to_json(),
            "machines": [{"name": self.host["name"], "games": results.totals()["games"]}],
            "pgn": None,
        } | ({"progress": schedule.progress(), "games": schedule.to_json()} if schedule else {})

    def to_json(self) -> dict:
        return {"match_id": self.match_id, "started_at": self.started_at, "name": self.name, "host": self.host,
                "settings": self.settings, "subject": self.subject, "opponents": self.opponents,
                "subject_id": self.subject_id}

    @classmethod
    def from_json(cls, data: dict) -> "Identity":
        found = cls.__new__(cls)
        for key in ("match_id", "started_at", "name", "host", "settings", "subject", "opponents", "subject_id"):
            setattr(found, key, data[key])
        return found
