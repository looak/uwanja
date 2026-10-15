"""The gauntlet file, TOML, read into plain dataclasses.

Unknown keys are an error naming the ones that exist, a typo shouldn't quietly play a different gauntlet. What an
engine may need beyond this goes in its `options` (UCI options, any name), `args` (its command line) or
`fastchess_args` (raw fastchess `-engine` tokens), and the whole run's in `[fastchess] args`. New settings are one
line in the *_KEYS tables and the dataclass.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TOP_KEYS = {"name", "tc", "book", "book_format", "book_order", "rounds", "concurrency", "adjudication", "recover", "pgn_options",
            "fastchess", "subject", "opponent"}
FASTCHESS_KEYS = {"version", "path", "args"}
ENGINE_KEYS = {"id", "name", "version", "url", "ref", "asset", "binary", "build", "cmd", "dir", "options", "args",
               "bench", "rating", "fastchess_args"}
ADJUDICATION_KEYS = {"win", "draw"}
RATING_KEYS = {"value", "list", "as_of"}

DEFAULT_FASTCHESS = "v1.8.2-alpha"      # the release the runner's log parsing was written against


class ConfigError(Exception):
    pass


@dataclass
class Rating:
    value: float
    list: str | None = None
    as_of: str | None = None

    def to_json(self) -> dict:
        return {"value": self.value, "list": self.list, "as_of": self.as_of}


@dataclass
class EngineSpec:
    role: str                              # subject | opponent
    id: str                                # unique in the match, also the name fastchess plays it under
    name: str | None = None                # overrides the engine's `id name`
    version: str | None = None             # overrides what `buildinfo` or the name says
    url: str | None = None                 # GitHub repository or release URL to fetch from
    ref: str | None = None                 # release tag, the latest release without
    asset: str | None = None               # glob over the release's asset names
    binary: str | None = None              # glob over paths inside the asset
    build: str | None = None               # how to build from source, not used yet
    cmd: str | None = None                 # a local executable, instead of url
    dir: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    args: list[str] = field(default_factory=list)
    bench: bool | None = None              # None: ask only an engine that answers `buildinfo`
    rating: Rating | None = None
    fastchess_args: list[str] = field(default_factory=list)


@dataclass
class Gauntlet:
    name: str
    tc: str
    subject: EngineSpec
    opponents: list[EngineSpec]
    rounds: int
    concurrency: int = 1
    book: str | None = None
    book_format: str | None = None
    book_order: str = "random"
    adjudication: dict[str, str] = field(default_factory=dict)
    pgn_options: tuple[str, ...] = ("nodes", "nps", "pv")   # what fastchess writes into move comments
    recover: bool = True                    # restart a crashed engine and go on, instead of stopping the gauntlet
    fastchess_version: str = DEFAULT_FASTCHESS
    fastchess_path: str | None = None
    fastchess_args: list[str] = field(default_factory=list)
    base: Path = Path(".")                  # relative paths in the file are relative to its directory
    source: Path | None = None

    @property
    def engines(self) -> list[EngineSpec]:
        return [self.subject, *self.opponents]

    def path(self, value: str | None) -> Path | None:
        return None if value is None else (self.base / value)


def load(path: str | Path) -> Gauntlet:
    path = Path(path)
    try:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    except OSError as error:
        raise ConfigError(f"{path}: {error}") from None
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from None
    return parse(raw, path.parent, path)


def parse(raw: dict, base: Path = Path("."), source: Path | None = None) -> Gauntlet:
    _only(raw, TOP_KEYS, "the gauntlet file")
    for key in ("tc", "subject"):
        if key not in raw:
            raise ConfigError(f"missing {key!r}")
    if not isinstance(raw.get("subject"), dict):
        raise ConfigError("[subject] must be a table")
    opponents = raw.get("opponent")
    if not isinstance(opponents, list) or not opponents:
        raise ConfigError("at least one [[opponent]] is needed")

    taken: set[str] = set()
    subject = _engine(raw["subject"], "subject", "subject", taken)
    others = [_engine(entry, "opponent", f"opponent {number}", taken) for number, entry in enumerate(opponents, 1)]

    fastchess = raw.get("fastchess", {})
    _only(fastchess, FASTCHESS_KEYS, "[fastchess]")
    adjudication = raw.get("adjudication", {})
    _only(adjudication, ADJUDICATION_KEYS, "[adjudication]")

    name = str(raw.get("name") or (source.stem if source else "gauntlet"))
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise ConfigError(f"name {name!r} may only hold letters, digits, '.', '_' and '-'")
    rounds = raw.get("rounds", 1)
    concurrency = raw.get("concurrency", 1)
    if not (isinstance(rounds, int) and rounds > 0 and isinstance(concurrency, int) and concurrency > 0):
        raise ConfigError("rounds and concurrency are whole numbers above zero")
    return Gauntlet(
        name=name, tc=str(raw["tc"]), subject=subject, opponents=others, rounds=rounds, concurrency=concurrency,
        book=raw.get("book"), book_format=raw.get("book_format"), book_order=raw.get("book_order", "random"),
        adjudication={key: str(value) for key, value in adjudication.items()}, recover=bool(raw.get("recover", True)), pgn_options=_pgn_options(raw.get("pgn_options")),
        fastchess_version=str(fastchess.get("version", DEFAULT_FASTCHESS)), fastchess_path=fastchess.get("path"),
        fastchess_args=_strings(fastchess.get("args", []), "[fastchess] args"), base=base, source=source)


def _pgn_options(value: Any) -> tuple[str, ...]:
    from .pgnout import EXTRAS
    if value is None:
        return ("nodes", "nps", "pv")
    options = _strings(value, "pgn_options")
    unknown = [name for name in options if name not in EXTRAS]
    if unknown:
        raise ConfigError(f"pgn_options: unknown {', '.join(map(repr, unknown))}; known: {', '.join(EXTRAS)}")
    return tuple(options)


def _only(table: Any, known: set[str], where: str) -> None:
    if not isinstance(table, dict):
        raise ConfigError(f"{where} must be a table")
    unknown = sorted(set(table) - known)
    if unknown:
        raise ConfigError(f"{where}: unknown {', '.join(map(repr, unknown))}; known: {', '.join(sorted(known))}")


def _strings(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, (str, int, float)) for item in value):
        raise ConfigError(f"{where} must be a list of strings")
    return [str(item) for item in value]


def _engine(table: dict, role: str, where: str, taken: set[str]) -> EngineSpec:
    _only(table, ENGINE_KEYS, f"[{where}]" if role == "subject" else f"[[opponent]] {where}")
    if bool(table.get("url")) == bool(table.get("cmd")):
        raise ConfigError(f"{where}: give exactly one of url (fetch it) or cmd (a local executable)")
    if role == "subject" and "rating" in table:
        raise ConfigError(f"{where}: the engine under test has no rating, ratings belong to opponents")
    ident = table.get("id") or _slug(table.get("name") or table.get("url") or table.get("cmd"))
    ident = _unique(ident, taken)
    taken.add(ident)
    return EngineSpec(
        role=role, id=ident, name=table.get("name"), version=_opt_str(table.get("version")), url=table.get("url"),
        ref=_opt_str(table.get("ref")), asset=table.get("asset"), binary=table.get("binary"), build=table.get("build"),
        cmd=table.get("cmd"), dir=table.get("dir"), options=dict(table.get("options", {})),
        args=_strings(table.get("args", []), f"{where} args"), bench=table.get("bench"),
        rating=_rating(table.get("rating"), where),
        fastchess_args=_strings(table.get("fastchess_args", []), f"{where} fastchess_args"))


def _rating(value: Any, where: str) -> Rating | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return Rating(float(value))
    _only(value, RATING_KEYS, f"{where} rating")
    if "value" not in value:
        raise ConfigError(f"{where}: rating needs a value")
    return Rating(float(value["value"]), value.get("list"), None if value.get("as_of") is None else str(value["as_of"]))


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _slug(text: str) -> str:
    """An id from a URL or a path: its last meaningful piece, `https://github.com/o/Barbarossa` is `barbarossa`."""
    text = re.sub(r"/releases/.*$|\.git$|\.exe$", "", str(text).rstrip("/"))
    piece = re.split(r"[/\\]", text)[-1]
    return re.sub(r"[^A-Za-z0-9._-]+", "-", piece).strip("-").lower() or "engine"


def _unique(ident: str, taken: set[str]) -> str:
    if ident not in taken:
        return ident
    number = 2
    while f"{ident}-{number}" in taken:
        number += 1
    return f"{ident}-{number}"
