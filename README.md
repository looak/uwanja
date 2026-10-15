# uwanja

*Swahili for a playing field, where the herd meets others.*

An engine-agnostic gauntlet runner. Give it a list of UCI engines, it fetches them (release binaries), plays a
gauntlet through [fastchess](https://github.com/Disservin/fastchess), streams every game to
[nyayo](https://github.com/looak/nyayo) while it's played, and sends the match, with its counts, as games finish and
at the end.

It knows nothing about any one engine: the engine under test is any UCI binary, as the opponents are. It runs the
same on Windows, Linux and macOS, Python 3.11+ with one dependency (python-chess).

A stop signal (Ctrl-C, SIGTERM, SIGHUP, Ctrl-Break) stops fastchess and reports the match `stopped`.

Status: working end to end against fastchess and a mock nyayo. Building engines from source, resuming a stopped
gauntlet and `selfplay` are not done, see Not yet. The design is in ramani's `plans/gauntlet-runner.md`.

## Using it

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt    # .venv/Scripts/pip on Windows; python-chess, py-cpuinfo, uwanja

uwanja run gauntlet.toml               # fetch, play, report
uwanja run gauntlet.toml --dry-run     # fetch and resolve everything, print fastchess' command line, play nothing
uwanja report runs/<dir>               # send a run's document and games to nyayo again
uwanja follow gauntlet.toml --log fc.log --pgn fc.pgn   # follow a recorded or replayed gauntlet, play nothing
uwanja replay fc.log live.log --pgn fc.pgn live.pgn     # a recorded log at its original pace, to develop against
uwanja host                            # what this machine is and which CPU builds it would pick
```

nyayo's address and token come from `NYAYO_URL` and `NYAYO_TOKEN`, or `git config nyayo.url` and `nyayo.token`, as
`results.py` takes them. Without them the gauntlet runs and keeps everything locally. `GITHUB_TOKEN` lifts GitHub's
rate limit for downloads and reaches private repositories.

Every run gets a directory, `runs/<name>-<timestamp>/`, holding the gauntlet file, the identity of what was played
(`identity.json`), the latest match document (`match.json`), every game's PGN (`games/<n>.pgn`), and fastchess' own
PGN, log and output. `uwanja report` sends it again from there.

## The gauntlet file

```toml
name = "elephant-1.0.0-ccrl-blitz"
tc = "120+1"
book = "books/8moves_v3.pgn"       # .epd or .pgn, the format follows the extension
rounds = 100                       # game pairs per opponent
concurrency = 14

[subject]                          # the engine under test
url = "https://github.com/looak/elephant"
ref = "1.0.0"                      # a release tag, the latest release without
options = { Threads = 1, Hash = 64 }

[[opponent]]
url = "https://github.com/<owner>/<engine>/releases/tag/<tag>"
rating = { value = 2465, list = "CCRL Blitz", as_of = "2026-10-01" }

[[opponent]]
cmd = "engines/older-elephant"     # a local executable instead of a download
options = { Hash = 64 }
rating = 2300                      # a number alone is fine, nyayo then has no list or date
```

An engine is a `url` (a GitHub repository, or a release's URL) or a `cmd`. Everything an engine might need beyond
that is in its table, nothing is hard-coded per engine:

| Key | |
|:--|:--|
| `id` | its name in the match and in fastchess, unique; from the URL or path when left out |
| `name`, `version` | override what the engine's `id name` and `buildinfo` say |
| `ref` | release tag; `v` is tried with and without |
| `asset` | glob over the release's asset names, when the picker gets it wrong |
| `binary` | glob over paths inside the asset, for releases that hold several executables |
| `options` | UCI options, any name: `Threads`, `Hash`, `Move Overhead`, ... |
| `args` | the engine's command line |
| `dir` | working directory, the executable's own directory without |
| `bench` | `true` or `false`; by default only engines that answer `buildinfo` are asked |
| `rating` | opponents only, a number or `{ value, list, as_of }` |
| `fastchess_args` | raw `-engine` tokens for fastchess, e.g. `["restart=on"]` |

Top level: `adjudication = { win = "movecount=3 score=400", draw = "movenumber=40 movecount=8 score=10" }`,
`pgn_options` (default `["nodes", "nps", "pv"]`, any of `nodes`, `seldepth`, `nps`, `hashfull`,
`tbhits`, `pv`: what fastchess writes into every move's comment, and what the live PGNs carry too),
`recover` (default `true`: a crashed engine is restarted and the game counted against it, instead of fastchess
stopping the whole gauntlet), `book_format`, `book_order` (default `random`), and `[fastchess]` with `version` (a release tag, `latest`, or
`system` for the one on PATH), `path` and `args` (appended to the command line). A key the runner doesn't know is an
error that lists the ones it does.

## What it does

1. **Fetches** fastchess (a pinned release, cached) and each engine's release asset for this OS, CPU architecture and
   instruction set (`vnni512`, `avx512`, `bmi2`, `avx2`, `sse41`, ...), read from the CPU's CPUID through py-cpuinfo,
   the best the CPU runs first. AMD
   before Zen 3 prefers `avx2` over `bmi2`, its PEXT is slow. `UWANJA_CPU=avx2` overrides detection. Downloads are
   cached by release and asset in `$UWANJA_HOME` or the OS's cache directory.
2. **Asks each engine who it is**: `uci` for its name, `buildinfo` for version and commit, `bench` for the node count,
   and records them in the match document.
3. **Plays** through fastchess: one gauntlet, the subject first, game pairs with colours swapped.
4. **Rebuilds each game as it's played** from fastchess' log, since its PGN comes only when a game ends, and sends the
   whole game to nyayo after every move, then fastchess' own PGN when it's over.
5. **Reports the match**: counts per opponent from the subject's side, pentanomial, time losses, crashes and illegal
   moves per side, ratings as given. It computes no statistics, that's nyayo's.
6. **Tells nyayo the whole schedule first**: before any game starts, the document lists every game as `scheduled`,
   then each moves to `playing` and `finished` (with result, termination and which side crashed, lost on time or
   moved illegally) from fastchess' own `Started game` and `Finished game` lines.

The match document is match schema v1 (ramani `plans/match-schema.md`), the games are PUT as PGN to
`/api/matches/{id}/games/{n}`.

## Layout

| File | |
|:--|:--|
| `uwanja/cli.py` | the commands |
| `uwanja/config.py` | the gauntlet file into dataclasses |
| `uwanja/hostinfo.py`, `assets.py` | OS, architecture and CPU tiers; which release asset is for this host |
| `uwanja/fetch.py`, `engines.py`, `fastchess.py` | downloading and unpacking, resolving and probing engines, fastchess' command line |
| `uwanja/events.py`, `uci.py`, `tracker.py` | fastchess log lines to UCI events to games (from bwawa) |
| `uwanja/schedule.py`, `pgnout.py`, `results.py`, `document.py` | the games before they're played, a game as PGN, counts from the PGN, the match document |
| `uwanja/nyayo.py`, `session.py` | the reporter thread, one run |
| `uwanja/replay.py` | a recorded log at its pace (from bwawa) |
| `tests/` | `tests/data` holds a recorded 16 game gauntlet (fastchess log and PGN) the live path runs against |

## Not yet

- Building an engine from source (`build = ...` is accepted and ignored: a release is needed).
- Resuming a stopped gauntlet. A stopped one reports `stopped`, a new run is a new match.
- `selfplay` through the same machinery.

## License

GPL-3.0, see [LICENSE](LICENSE). The log parser, tracker and replay tool come from bwawa.
