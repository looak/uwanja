"""From an engine in the config to an executable and what it says it is."""
from __future__ import annotations

import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import EngineSpec, Gauntlet
from .fetch import FetchError, fetch_release_binary
from .hostinfo import HostInfo


@dataclass
class Resolved:
    spec: EngineSpec
    cmd: Path
    dir: Path
    origin: dict                                       # where it came from, kept in the run directory
    id_name: str | None = None                         # UCI `id name`
    buildinfo: dict[str, str] = field(default_factory=dict)
    bench_nodes: int | None = None

    @property
    def name(self) -> str | None:
        return self.spec.name or _split_name(self.id_name)[0]

    @property
    def version(self) -> str | None:
        return self.spec.version or self.buildinfo.get("version") or _split_name(self.id_name)[1]

    @property
    def commit(self) -> str | None:
        return self.buildinfo.get("commit")

    @property
    def branch(self) -> str | None:
        return self.buildinfo.get("branch")


_VERSION = re.compile(r"^v?\d+(\.\d+)*([-+._]?[0-9A-Za-z]+)*$")


def _split_name(id_name: str | None) -> tuple[str | None, str | None]:
    """`Elephant Gambit 1.0.0` is ("Elephant Gambit", "1.0.0"); a name without a version keeps it None."""
    if not id_name:
        return None, None
    tokens = id_name.split()
    if len(tokens) > 1 and _VERSION.match(tokens[-1]) and any(c.isdigit() for c in tokens[-1]):
        return " ".join(tokens[:-1]), tokens[-1]
    return id_name, None


def resolve(gauntlet: Gauntlet, spec: EngineSpec, host: HostInfo, log=print) -> Resolved:
    """The executable for an engine: its local `cmd`, or the release asset for this host."""
    if spec.cmd:
        cmd = Path(spec.cmd)
        if not cmd.is_absolute():
            cmd = (gauntlet.base / cmd)
        cmd = cmd.resolve()
        if not cmd.exists():
            raise FetchError(f"{spec.id}: {cmd} doesn't exist")
        return Resolved(spec, cmd, Path(spec.dir).resolve() if spec.dir else cmd.parent, {"kind": "local"})
    try:
        fetched = fetch_release_binary(spec.url, spec.ref, host, asset=spec.asset, binary=spec.binary, log=log)
    except FetchError as error:
        if spec.build:
            raise FetchError(f"{spec.id}: {error}; building from source isn't supported yet "
                             f"(build = {spec.build!r} is ignored)") from None
        raise FetchError(f"{spec.id}: {error}") from None
    origin = {"kind": "release", "url": f"https://github.com/{fetched.owner}/{fetched.repo}", "tag": fetched.tag,
              "asset": fetched.asset, "published_at": fetched.published_at}
    return Resolved(spec, fetched.exe, Path(spec.dir).resolve() if spec.dir else fetched.exe.parent, origin)


_PROMPT = re.compile(r"^[>\s]+")


class _Engine:
    """A UCI engine process for a few questions, with a reader thread so waiting for an answer can time out on any
    OS."""

    def __init__(self, cmd: Path, cwd: Path):
        self.process = subprocess.Popen([str(cmd)], cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                        bufsize=1)
        self.lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.process.stdout:
            # some engines put a prompt or a log marker in front of every line, "> id name ..."
            self.lines.put(_PROMPT.sub("", line.strip()))
        self.lines.put(None)

    def ask(self, command: str, until: str, timeout: float) -> list[str] | None:
        """The lines sent back for `command` up to the one starting with `until`, None if it didn't come in time."""
        try:
            self.process.stdin.write(command + "\n")
            self.process.stdin.flush()
        except OSError:
            return None
        got: list[str] = []
        deadline = time.monotonic() + timeout
        while True:
            try:
                line = self.lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                return None
            if line is None:
                return None
            got.append(line)
            if line.startswith(until):
                return got

    def close(self) -> None:
        try:
            self.process.stdin.write("quit\n")
            self.process.stdin.flush()
            self.process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            self.process.kill()
        try:
            self.process.stdin.close()
        except OSError:
            pass


def probe(engine: Resolved, timeout: float = 15.0, bench_timeout: float = 120.0) -> Resolved:
    """Asks the engine who it is. `uci` for the `id name`, `buildinfo` (an unknown command is ignored, so any engine
    can be asked) for version, commit and the like, and `bench` for engines that answered `buildinfo`, or by
    `bench = true`. What can't be read stays empty, the config's name and version fill in."""
    try:
        process = _Engine(engine.cmd, engine.dir)
    except OSError as error:
        raise FetchError(f"{engine.spec.id}: can't start {engine.cmd}: {error}") from None
    try:
        reply = process.ask("uci", "uciok", timeout)
        if reply is None:
            raise FetchError(f"{engine.spec.id}: {engine.cmd} didn't answer `uci`, it isn't a UCI engine?")
        for line in reply:
            if line.startswith("id name "):
                engine.id_name = line[len("id name "):].strip()
        reply = process.ask("buildinfo\nisready", "readyok", timeout) or []
        for line in reply:
            match = re.match(r"buildinfo\s+(\S+)\s*(.*)$", line)
            if match:
                engine.buildinfo[match[1]] = match[2].strip()
        wants_bench = engine.spec.bench if engine.spec.bench is not None else bool(engine.buildinfo)
        if wants_bench:
            engine.bench_nodes = _bench(process, bench_timeout)
    finally:
        process.close()
    return engine


def _bench(process: _Engine, timeout: float) -> int | None:
    """`bench`'s node count, from a line like `1234567 nodes 2345678 nps`; engines print variations of that."""
    deadline = time.monotonic() + timeout
    try:
        process.process.stdin.write("bench\n")
        process.process.stdin.flush()
    except OSError:
        return None
    nodes = None
    while time.monotonic() < deadline:
        try:
            line = process.lines.get(timeout=1.0)
        except queue.Empty:
            continue
        if line is None:
            break
        match = re.search(r"(\d+)\s+nodes\s+(\d+)\s+nps", line, re.IGNORECASE) or \
            re.search(r"nodes\s*(?:searched)?\s*:?\s*(\d+)", line, re.IGNORECASE)
        if match:
            nodes = int(match[1])
            break
    return nodes
