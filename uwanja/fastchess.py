"""fastchess: finding it, fetching it, and the command line a gauntlet becomes."""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Gauntlet
from .engines import Resolved
from .fetch import FetchError, fetch_release_binary
from .hostinfo import HostInfo

REPO = "https://github.com/Disservin/fastchess"


@dataclass
class Fastchess:
    exe: Path
    version: str


def locate(gauntlet: Gauntlet, host: HostInfo, log=print) -> Fastchess:
    """The fastchess to use: `[fastchess] path`, the one on PATH for `version = "system"`, else the release asset
    for this OS and CPU, cached."""
    if gauntlet.fastchess_path:
        exe = gauntlet.path(gauntlet.fastchess_path).resolve()
        if not exe.exists():
            raise FetchError(f"[fastchess] path {exe} doesn't exist")
        return Fastchess(exe, _version(exe))
    if gauntlet.fastchess_version == "system":
        found = shutil.which("fastchess")
        if found is None:
            raise FetchError("version = \"system\" but there's no fastchess on PATH")
        return Fastchess(Path(found), _version(Path(found)))
    fetched = fetch_release_binary(REPO, gauntlet.fastchess_version, host, kind="fastchess", binary=None, log=log)
    return Fastchess(fetched.exe, fetched.tag)


def _version(exe: Path) -> str:
    """`fastchess alpha 1.8.2 compiled for ...` is 1.8.2-alpha; whatever it prints if it's another shape."""
    try:
        out = subprocess.run([str(exe), "--version"], capture_output=True, encoding="utf-8", errors="replace",
                             timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    found = re.search(r"(?:(alpha|beta)\s+)?v?(\d+\.\d+(?:\.\d+)?)", out)
    if found is None:
        return out.splitlines()[0] if out else "unknown"
    return f"{found[2]}-{found[1]}" if found[1] else found[2]


def book_format(gauntlet: Gauntlet) -> str | None:
    if gauntlet.book_format:
        return gauntlet.book_format
    suffix = Path(gauntlet.book or "").suffix.lower().lstrip(".")
    return suffix if suffix in ("epd", "pgn") else None


def command(gauntlet: Gauntlet, fastchess: Fastchess, engines: list[Resolved], pgn: Path, log: Path,
            config_out: Path) -> list[str]:
    """argv for the gauntlet: the subject first (fastchess' gauntlet takes the first engine as the seed), one
    `-engine` each, rounds of two games with the same opening and colours swapped."""
    argv = [str(fastchess.exe)]
    for engine in engines:
        spec = engine.spec
        argv += ["-engine", f"cmd={engine.cmd}", f"name={spec.id}", f"dir={engine.dir}"]
        if spec.args:
            argv.append("args=" + " ".join(spec.args))
        for key, value in spec.options.items():
            argv.append(f"option.{key}={_option(value)}")
        argv += spec.fastchess_args
    argv += ["-each", f"tc={gauntlet.tc}",
             "-tournament", "gauntlet", "-rounds", str(gauntlet.rounds), "-games", "2", "-repeat",
             "-concurrency", str(gauntlet.concurrency)]
    if gauntlet.book:
        opening = ["-openings", f"file={gauntlet.path(gauntlet.book).resolve()}", f"order={gauntlet.book_order}"]
        fmt = book_format(gauntlet)
        if fmt:
            opening.append(f"format={fmt}")
        argv += opening
    if gauntlet.adjudication.get("win"):
        argv += ["-resign", *gauntlet.adjudication["win"].split()]
    if gauntlet.adjudication.get("draw"):
        argv += ["-draw", *gauntlet.adjudication["draw"].split()]
    if gauntlet.recover:
        argv.append("-recover")
    argv += ["-pgnout", f"file={pgn}", *(f"{name}=true" for name in gauntlet.pgn_options),
             "-log", f"file={log}", "level=info", "engine=true", "realtime=true",
             "-config", f"outname={config_out}"]
    return argv + gauntlet.fastchess_args


def _option(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
