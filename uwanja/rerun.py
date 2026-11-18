"""The config that goes with a match, and a new match started from a stored one.

`config` is the producer's own block in every match document (nyayo's match schema, *The config*): `format` says who
reads it, `gauntlet` is what was asked for as the gauntlet file in JSON with its defaults filled in (`config.parse`
takes it back as it is), `resolved` is what that became on the machine that played (tags, assets, the SHA-256 of every
binary and of the book), `portable` is false when the file names things only that machine has. It holds no token, no
environment variable, nyayo's address or a book's path: the book goes by name and hash.
"""
from __future__ import annotations

import html
import re
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from .config import ConfigError, EngineSpec, Gauntlet, parse
from .document import sha256
from .engines import Resolved
from .fastchess import Fastchess, book_format

FORMAT = "uwanja.gauntlet/1"


def _engine(spec: EngineSpec) -> dict:
    data = {"id": spec.id}
    for key in ("name", "version", "url", "ref", "asset", "binary", "build", "cmd", "dir", "bench"):
        value = getattr(spec, key)
        if value is not None:
            data[key] = value
    for key in ("options", "args", "fastchess_args"):
        value = getattr(spec, key)
        if value:
            data[key] = list(value) if isinstance(value, list) else dict(value)
    if spec.rating is not None:
        data["rating"] = spec.rating.to_json()
    return data


def gauntlet_json(gauntlet: Gauntlet) -> dict:
    """The gauntlet file as JSON, defaults filled in. A book is its file's name, wherever the file was."""
    data: dict = {"name": gauntlet.name, "tc": gauntlet.tc, "rounds": gauntlet.rounds,
                  "concurrency": gauntlet.concurrency}
    if gauntlet.book:
        data |= {"book": Path(gauntlet.book).name, "book_format": book_format(gauntlet),
                 "book_order": gauntlet.book_order}
    if gauntlet.adjudication:
        data["adjudication"] = dict(gauntlet.adjudication)
    data |= {"recover": gauntlet.recover, "pgn_options": list(gauntlet.pgn_options)}
    fastchess = {"version": gauntlet.fastchess_version}
    if gauntlet.fastchess_path:
        fastchess["path"] = gauntlet.fastchess_path
    if gauntlet.fastchess_args:
        fastchess["args"] = list(gauntlet.fastchess_args)
    data["fastchess"] = fastchess
    data["subject"] = _engine(gauntlet.subject)
    data["opponent"] = [_engine(spec) for spec in gauntlet.opponents]
    return {key: value for key, value in data.items() if value is not None}


def _resolved_engine(engine: Resolved) -> dict:
    data = {"id": engine.spec.id, **{key: engine.origin[key] for key in ("kind", "url", "tag", "asset", "published_at")
                                     if engine.origin.get(key)}}
    if engine.origin.get("kind") == "recorded":
        return data
    data |= {"sha256": sha256(engine.cmd), "name": engine.id_name, "version": engine.version, "commit": engine.commit}
    return {key: value for key, value in data.items() if value is not None}


def config_block(gauntlet: Gauntlet, runner: Fastchess, engines: list[Resolved]) -> dict:
    """What goes into every document as `config`."""
    book = gauntlet.path(gauntlet.book)
    resolved = {
        "runner": {"name": "fastchess", "version": runner.version, "sha256": sha256(runner.exe)},
        "subject": _resolved_engine(engines[0]),
        "opponents": [_resolved_engine(engine) for engine in engines[1:]],
    }
    if gauntlet.book:
        resolved["book"] = {"name": Path(gauntlet.book).name, "sha256": sha256(book)}
    local = (any(spec.cmd or spec.dir for spec in gauntlet.engines) or bool(gauntlet.fastchess_path))
    return {"format": FORMAT, "gauntlet": gauntlet_json(gauntlet), "resolved": resolved, "portable": not local}


# --- run --from-match ---

class RerunError(ConfigError):
    pass


_CONFIG_FIELD = re.compile(r'<textarea\b[^>]*\bid="copy-match-config"[^>]*>(.*?)</textarea>', re.DOTALL)


def match_url(reference: str, nyayo_url: str | None) -> str:
    """A match's page: the URL as given, or a match id on the nyayo that's configured."""
    if re.match(r"https?://", reference):
        return reference
    if not nyayo_url:
        raise RerunError(f"{reference!r} is not a URL and no nyayo is configured (NYAYO_URL) to take it as a match id")
    return nyayo_url.rstrip("/") + "/matches/" + reference


def fetch_page(url: str, token: str | None = None, nyayo_url: str | None = None) -> str:
    """The match page's HTML. The token goes only to the nyayo it belongs to."""
    request = urllib.request.Request(url, headers={"User-Agent": "uwanja"})
    if token and nyayo_url and urlsplit(url)[:2] == urlsplit(nyayo_url)[:2]:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as answer:
            return answer.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        raise RerunError(f"{url}: nyayo answered {error.code}") from None
    except (urllib.error.URLError, OSError) as error:
        raise RerunError(f"{url}: {getattr(error, 'reason', error)}") from None


def gauntlet_file_from_page(page: str) -> str:
    """The gauntlet file nyayo shows on a match's page (*Copy as gauntlet.toml*, built from the stored `config`)."""
    found = _CONFIG_FIELD.search(page)
    if found is None:
        raise RerunError("the page has no stored gauntlet: the match has no `config` (it wasn't played by a uwanja "
                         "that stores it), or this is not a match page")
    return html.unescape(found[1]).strip() + "\n"


def find_book(name: str, base: Path) -> Path | None:
    for folder in (base / "books", base.parent / "books", base):
        if (folder / name).is_file():
            return (folder / name).resolve()
    return None


def gauntlet_from_page(page: str, base: Path) -> tuple[Gauntlet, str]:
    """A gauntlet to play again from a match's page, and its file's text (for the run directory). Relative paths in
    it are relative to `base`; the book is looked for by its name, in `books/` here or next to here."""
    text = gauntlet_file_from_page(page)
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise RerunError(f"the stored gauntlet isn't a gauntlet file: {error}") from None
    if raw.get("book"):
        name = Path(raw["book"]).name
        book = find_book(name, base)
        if book is None:
            raise RerunError(f"the match was played with the book {name}: put it in {base / 'books'}")
        raw["book"] = str(book)
    return parse(raw, base), text
