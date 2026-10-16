"""uwanja's command line.

    uwanja run gauntlet.toml [--out DIR] [--no-report] [--dry-run]
    uwanja follow gauntlet.toml --log FILE --pgn FILE      play nothing, follow a recorded or replayed gauntlet
    uwanja report RUN_DIR [--config gauntlet.toml]         send a run's document and games to nyayo again
    uwanja replay SOURCE TARGET [--pgn SRC DST]            a recorded log at its pace, to develop `follow` against
    uwanja host                                            what this machine is, and which builds it'd pick
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import uuid
from pathlib import Path

from . import __version__, fastchess as fastchess_mod
from .config import ConfigError, Gauntlet, load
from .document import Identity
from .engines import Resolved, probe, resolve
from .fetch import FetchError, cache_dir
from .hostinfo import HostInfo, detect
from .nyayo import Reporter, nyayo_config
from .pgnout import read_games, to_finished
from .results import Results
from .session import RunPaths, Session


def say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _engines(gauntlet: Gauntlet, host: HostInfo, fetch: bool = True) -> list[Resolved]:
    found = []
    for spec in gauntlet.engines:
        say(f"uwanja: {spec.role} {spec.id}: {spec.cmd or spec.url + (' @ ' + spec.ref if spec.ref else '')}")
        engine = resolve(gauntlet, spec, host, log=say)
        say(f"uwanja:   {engine.cmd}")
        found.append(probe(engine))
        missing = [key for key in spec.options if engine.declared and key not in engine.declared]
        if missing:
            say(f"uwanja:   warning: {engine.id_name or spec.id} doesn't list the option(s) "
                f"{', '.join(missing)}; fastchess sends them anyway"
                f" (it has: {', '.join(sorted(engine.declared)) or 'none'})")
        say(f"uwanja:   {engine.id_name or '?'}"
            + (f", {engine.buildinfo.get('commit', '')[:10]}" if engine.buildinfo.get("commit") else "")
            + (f", bench {engine.bench_nodes}" if engine.bench_nodes else ""))
    return found


def _stub(gauntlet: Gauntlet) -> list[Resolved]:
    """Engines that were never started, for following a gauntlet someone else played: the config's names stand in."""
    return [Resolved(spec, Path(spec.cmd or spec.id), Path("."), {"kind": "recorded"}, spec.name or spec.id)
            for spec in gauntlet.engines]


def cmd_run(args: argparse.Namespace) -> int:
    gauntlet = load(args.config)
    host = detect()
    say(f"uwanja {__version__} on {host.os} {host.arch}, {host.cpu_name or 'unknown CPU'}")
    runner = fastchess_mod.locate(gauntlet, host, log=say)
    say(f"uwanja: fastchess {runner.version}: {runner.exe}")
    engines = _engines(gauntlet, host)

    root = Path(args.out or "runs") / f"{gauntlet.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    paths = RunPaths(root)
    argv = fastchess_mod.command(gauntlet, runner, engines, paths.pgn, paths.log, paths.fastchess_config)
    if args.dry_run:
        say("uwanja: would run (dry run, nothing played):")
        print(" ".join(f'"{token}"' if " " in token else token for token in argv))
        return 0
    root.mkdir(parents=True)
    shutil.copy2(gauntlet.source, paths.config)
    identity = Identity(str(uuid.uuid4()), gauntlet, engines[0], engines[1:], host, runner.version)
    paths.identity.write_text(json.dumps(identity.to_json(), indent=2) + "\n", encoding="utf-8")
    return Session(gauntlet, identity, paths, _reporter(args, identity.match_id), say).run(argv)


def cmd_follow(args: argparse.Namespace) -> int:
    gauntlet = load(args.config)
    root = Path(args.out or "runs") / f"{gauntlet.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    paths = RunPaths(root)
    root.mkdir(parents=True)
    shutil.copy2(gauntlet.source, paths.config)
    # the files are followed where they are, the run directory only holds what's reported
    paths_log, paths_pgn = Path(args.log), Path(args.pgn)
    engines = _stub(gauntlet)
    identity = Identity(str(uuid.uuid4()), gauntlet, engines[0], engines[1:], detect(), "recorded")
    paths.identity.write_text(json.dumps(identity.to_json(), indent=2) + "\n", encoding="utf-8")
    session = Session(gauntlet, identity, paths, _reporter(args, identity.match_id), say)
    session._log_follow.path, session._pgn_follow.follower.path = paths_log, paths_pgn
    return session.run(None, idle_exit=args.idle_exit)


def cmd_report(args: argparse.Namespace) -> int:
    paths = RunPaths(Path(args.run_dir))
    url, token = nyayo_config()
    if not url:
        say("uwanja: nyayo isn't configured (NYAYO_URL / git config nyayo.url), nothing to send to")
        return 1
    if paths.identity.exists():
        identity = Identity.from_json(json.loads(paths.identity.read_text(encoding="utf-8")))
    else:      # a gauntlet from before the runner: its config and fastchess' PGN are what there is
        config = Path(args.config) if args.config else paths.config
        if not config.exists() or not paths.pgn.exists():
            say(f"uwanja: {paths.root} has neither identity.json nor a gauntlet.toml and games.pgn to rebuild it from")
            return 1
        gauntlet = load(config)
        engines = _stub(gauntlet)
        identity = Identity(str(uuid.uuid4()), gauntlet, engines[0], engines[1:], detect(), "unknown")
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.identity.write_text(json.dumps(identity.to_json(), indent=2) + "\n", encoding="utf-8")
    reporter = Reporter(url, token, identity.match_id, say)
    reporter.start()
    results = Results(identity.subject_id, [opponent["id"] for opponent in identity.opponents])
    sent = 0
    if paths.pgn.exists():
        for game, text in read_games(paths.pgn.read_text(encoding="utf-8", errors="replace")):
            entry = to_finished(game, text)
            results.add(entry.white, entry.black, entry.result, entry.termination, entry.comment, entry.round)
    for number, text in sorted(((int(p.stem), p.read_text(encoding="utf-8")) for p in paths.games.glob("*.pgn")
                                if p.stem.isdigit()), key=lambda pair: pair[0]) if paths.games.exists() else ():
        reporter.put_game(number, text, final='[Result "*"]' not in text)
        sent += 1
    if paths.match.exists() and not paths.pgn.exists():
        document = json.loads(paths.match.read_text(encoding="utf-8"))
    else:
        state = json.loads(paths.match.read_text(encoding="utf-8"))["match"]["state"] if paths.match.exists() else "stopped"
        document = identity.document(results, state if state != "running" else "stopped", None)
        if paths.match.exists():     # the original finish time stays
            document["match"]["finished_at"] = json.loads(paths.match.read_text(encoding="utf-8"))["match"].get("finished_at")
    reporter.put_match(document)
    reporter.close()
    say(f"uwanja: sent match {identity.match_id} and {sent} games"
        + (f", last error: {reporter.failed}" if reporter.failed else ""))
    return 1 if reporter.failed else 0


def cmd_host(args: argparse.Namespace) -> int:
    host = detect()
    from .hostinfo import TIERS
    used = set().union(*(needs for tiers in TIERS.values() for _, _, needs in tiers))
    print(json.dumps({
        "os": host.os, "arch": host.arch, "cpu": host.cpu_name, "logical_cores": host.logical_cores,
        "flags": sorted(host.flags & used), "slow_pext": host.slow_pext,
        "tiers": [tier for tier, _ in host.tiers()], "cache": str(cache_dir()),
    }, indent=2))
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    from . import replay
    return replay.main(args.rest)


def _reporter(args: argparse.Namespace, match_id: str) -> Reporter | None:
    if args.no_report:
        return None
    url, token = nyayo_config()
    if not url:
        say("uwanja: nyayo isn't configured (NYAYO_URL, or git config nyayo.url), the run stays local")
        return None
    say(f"uwanja: reporting match {match_id} to {url}")
    return Reporter(url, token, match_id, say)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="uwanja", description="Gauntlets through fastchess, reported to nyayo.")
    parser.add_argument("--version", action="version", version=f"uwanja {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="fetch engines and fastchess, play the gauntlet, report it")
    run.add_argument("config", help="the gauntlet file (TOML)")
    run.add_argument("--out", metavar="DIR", help="where run directories go (default ./runs)")
    run.add_argument("--no-report", action="store_true", help="don't send anything to nyayo")
    run.add_argument("--dry-run", action="store_true", help="fetch and resolve everything, print fastchess' command line")
    run.set_defaults(func=cmd_run)

    follow = commands.add_parser("follow", help="follow a recorded or replayed gauntlet's log and PGN, play nothing")
    follow.add_argument("config", help="the gauntlet file; engines are named as fastchess named them in the log")
    follow.add_argument("--log", required=True, metavar="FILE", help="fastchess log (-log ... engine=true realtime=true)")
    follow.add_argument("--pgn", required=True, metavar="FILE", help="fastchess PGN")
    follow.add_argument("--out", metavar="DIR", help="where run directories go (default ./runs)")
    follow.add_argument("--no-report", action="store_true")
    follow.add_argument("--idle-exit", type=float, metavar="SECONDS", default=30.0,
                        help="stop when nothing new came for this long and the log didn't say it finished (default 30)")
    follow.set_defaults(func=cmd_follow)

    report = commands.add_parser("report", help="send a run directory's match and games to nyayo again")
    report.add_argument("run_dir")
    report.add_argument("--config", metavar="FILE", help="gauntlet file for a directory that has none (older gauntlets)")
    report.set_defaults(func=cmd_report)

    commands.add_parser("host", help="print the detected OS, CPU, and the build tiers it would take").set_defaults(
        func=cmd_host)

    replay = commands.add_parser("replay", help="replay a recorded log at its pace (arguments as in uwanja.replay)")
    replay.add_argument("rest", nargs=argparse.REMAINDER)
    replay.set_defaults(func=cmd_replay)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, FetchError, ValueError) as error:
        say(f"uwanja: {error}")
        return 2
    except KeyboardInterrupt:
        return 130
