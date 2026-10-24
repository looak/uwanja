# uwanja

Repo: [looak/uwanja](https://github.com/looak/uwanja) · Python 3.11+ (python-chess, py-cpuinfo), Windows, Linux and
macOS · runs where a gauntlet is played

This file says what the code does, and is changed in the same pull request as the code. Where it is going (planned
work, open decisions) is in ramani's `uwanja.md`, and the plans that span repos are in its `plans/`.

## Purpose

Run a gauntlet, one engine against a list of opponents, and report it to nyayo. fastchess plays the games; uwanja
turns a gauntlet description into a fastchess invocation, fetches what's needed to play it, and turns fastchess's
output into a match document (nyayo's [match schema](https://github.com/looak/nyayo/blob/main/docs/match-schema.md))
and a stream of games, every game sent to nyayo move by move.

It knows nothing about Elephant: the engine under test is any UCI binary, as the opponents are.

## Scope

**Does:** fetch fastchess and each engine's release binary for the host's OS, CPU architecture and instruction set;
ask each engine who it is (`id name`, `buildinfo`, `bench`); play the gauntlet; tell nyayo the whole schedule in the
first report and then each entry that changes; rebuild games in progress from fastchess's log; place each finished
game from its own PGN; count results from the subject's side; keep everything reported in a run directory so it can be
sent again.

**Doesn't:** play games itself (fastchess), compute statistics (counts go to nyayo, which computes Elo, LLR and
performance), fetch ratings (they come from the gauntlet file), validate time controls, books or engine paths (they're
fastchess's), build engines from source, resume a stopped gauntlet, or show anything: nyayo does.

## Current

### The gauntlet file

TOML. `[subject]` is the engine under test, each `[[opponent]]` one of its opponents. An engine is a `url` (a GitHub
repository, or a release URL) or a `cmd` (a local executable), exactly one. `gauntlets/` holds the CCRL blitz gauntlets
of Elephant 1.0.0 and 1.2.0 as examples.

| Top level | |
|:--|:--|
| `name` | the match's name, also its run directory's; the file's name without |
| `tc`, `book`, `rounds`, `concurrency` | as fastchess takes them; `rounds` is game *pairs* per opponent |
| `book_format`, `book_order` | `.epd` and `.pgn` by extension, `random` order |
| `adjudication` | `{ win, draw }`, fastchess' `-resign` and `-draw` options as one string each |
| `recover` | default `true`, fastchess restarts a crashed engine and counts the game against it |
| `pgn_options` | default `["nodes", "nps", "pv"]`; of `nodes`, `seldepth`, `nps`, `hashfull`, `tbhits`, `pv`, what fastchess's `-pgnout` writes into each move's comment (see Move comments) |
| `[fastchess]` | `version` (a release tag, `latest`, or `system` for PATH), `path`, `args` |

| Engine key | |
|:--|:--|
| `id` | its name in the match and in fastchess, unique; from the URL or path without |
| `name`, `version` | override what `id name` and `buildinfo` say |
| `ref`, `asset`, `binary` | the release tag (latest without), a glob over asset names, a glob over paths in the asset |
| `options`, `args`, `dir` | UCI options (any name), the engine's command line, its working directory |
| `bench` | `true` or `false`; by default only an engine that answers `buildinfo` is asked |
| `rating` | opponents only, a number or `{ value, list, as_of }`, from the file and never fetched |
| `fastchess_args` | raw `-engine` tokens for fastchess |
| `build` | accepted and ignored, engines are not built from source |

Unknown keys are an error listing the known ones. Relative paths are relative to the file. The keys are the tables at
the top of `config.py`.

### Commands

| | |
|:--|:--|
| `uwanja run FILE [--out DIR] [--no-report] [--dry-run]` | fetch, probe, play, report. `--dry-run` resolves everything and prints fastchess' command line. An option an engine doesn't list in its `uci` answer is warned about |
| `uwanja follow FILE --log F --pgn F [--idle-exit S]` | play nothing, follow a recorded or replayed fastchess log and PGN; ends at fastchess's finish line, or after `S` seconds (30) without news |
| `uwanja replay SRC DST [--pgn SRC DST]` | a recorded log at its original pace, to develop `follow` against |
| `uwanja report DIR [--config FILE]` | send a run directory's document and games again, all whole |
| `uwanja rebuild DIR` | redo a run directory's games and document from fastchess' PGN, keeping the old ones as `*.before-rebuild` (see Placing finished games) |
| `uwanja host` | the detected OS, CPU, flags and the build tiers it would take |

Exit codes: 0 a finished gauntlet, 130 a stopped one, 1 a gauntlet that ended in error, 2 a configuration, download or
value error.

nyayo's address and token: `NYAYO_URL` and `NYAYO_TOKEN`, or `git config nyayo.url` and `nyayo.token`, as `results.py`
takes them; without them the gauntlet runs and keeps everything locally. `GITHUB_TOKEN` lifts GitHub's rate limit and
reaches private repositories. `UWANJA_HOME` moves the cache, `UWANJA_CPU` overrides CPU detection with a tier.

### Getting binaries

A GitHub release's assets are picked by name: the OS, the architecture and an instruction set tier are read from it
(`hostinfo.TIERS`, `assets.py`, both plain tables), assets for another OS, architecture or a tier the host can't run
are dropped, and the one the host prefers wins. On x86-64 the order is `vnni512`, `avx512`, `x86-64-v4`, `bmi2`,
`x86-64-v3`, `avx2`, `sse41`, `x86-64-v2`, `ssse3`, a build without a tier last (the `x86-64-v*` names are the
microarchitecture levels Elephant's releases use). AMD before Zen 3 puts `bmi2` behind the others, PEXT is slow there.
On arm64: `dotprod`, `neon`.

Detection on x86-64: CPUID through py-cpuinfo, on every OS, a dependency. Without it (and on arm64) the OS is read:
`/proc/cpuinfo` on Linux, `sysctl` on macOS, `IsProcessorFeaturePresent` on Windows, which can't say BMI2 or the
AVX-512 subsets: AVX2 implies BMI2, AVX512F implies BW and VL, VNNI is never claimed there.

Downloads are cached by release and asset in `$UWANJA_HOME/cache`, else the OS's cache directory (for a Python from
the Microsoft Store, `~/.uwanja/cache`, because the Store virtualises AppData and some engines can't resolve their
own path there); archives (zip, tar, tar.gz, xz, bz2) are unpacked, the executable is found by extension and name,
`binary` says which when there are several. fastchess is pinned (`v1.8.2-alpha`, the release the log parser was
written against) and fetched the same way.

### What a run does

1. Resolve fastchess and every engine, then probe each with `uci`, `buildinfo` and (where it answered `buildinfo`)
   `bench`: `id name`, version, commit, branch and bench node count go into the match document. An engine that
   doesn't answer `buildinfo` is fine, the config's `name` and `version` fill in.
2. Create `runs/<name>-<timestamp>/` and a match id (a uuid, fixed for the run).
3. Send the first document: state `running`, every game `scheduled`, counts at zero, whole.
4. Start fastchess: `-tournament gauntlet`, the subject first, `-games 2 -repeat`, `-pgnout`, `-log level=info
   engine=true realtime=true`, `-config outname=`, `-recover` unless turned off.
5. Follow three files on one thread: fastchess's stdout (`Started game N of M (A vs B)` and `Finished game N (A vs B):
   R {reason}`, which set a game's state, termination and fault), its log (UCI traffic, from which the tracker rebuilds
   every game in progress) and its PGN (the game as fastchess wrote it, which seals the game and feeds the counts).
6. Two sender threads send to nyayo, one for the match document and one for the games (see Reporting). When fastchess
   exits, the run's last document goes with the state: `finished` on exit code 0, `stopped` on Ctrl-C, SIGTERM,
   SIGHUP or Ctrl-Break (fastchess is stopped), `error` otherwise. It is whole, and it goes after every game handed
   over before it. A signal that can't be caught (SIGKILL, power) leaves nyayo showing `running`.

The schedule: game N of `rounds × 2 × opponents` is a function of the config, round by round, each opponent's pair
(subject with white first, then with black) on one opening. fastchess's own `Started game` line has the last word
on who has white while a game is played.

### Placing finished games

A finished game's slot in the schedule is not matched from the log. fastchess's PGN `Round` tag is the pair (a round
holds games `2r-1` and `2r`) and the colours say which of the two, so `Schedule.locate` needs no timing and no order,
and a game whose last move the log never showed (fastchess logs some engines' output under the wrong thread) is
placed all the same. When fastchess had the colours the other way round from the schedule's guess, the entry takes the
PGN's. A game the schedule has no slot for is appended.

`uwanja rebuild DIR` does this to a finished run directory from `games.pgn` alone, for a run whose schedule came out
wrong or a gauntlet uwanja didn't run; a slot played twice (a resume) counts its first game, and `uwanja report DIR`
then sends the result.

### Reporting

nyayo's [match schema](https://github.com/looak/nyayo/blob/main/docs/match-schema.md) owns the document and the
requests (*Reports by changes*, *Moves by changes*); this is what uwanja does with it. The match document and the
games are sent by two threads, each with its own requests (a new connection each), so a report never waits behind a
round's games, and a game route that is slow or failing leaves the reports going.

| What | When | How |
|:--|:--|:--|
| the match document | first, then when a game starts or ends (at most every 2 s) and every 30 s | `PUT /api/matches/{id}`, gzipped JSON, with `report.seq` and `report.base` (below) |
| a game in progress | at every change: it started, a move was made | `PUT .../games/{n}`, the whole PGN so far, `[Result "*"]`, fastchess-style move comments; once nyayo has said how many plies it holds, `PATCH` with the moves after them |
| a finished game | when fastchess's PGN has it | `PUT` with fastchess's own PGN, whole, last; later states of it are dropped |
| the run's last document | when the run ends | `PUT`, whole, after every game handed over before it; `close()` waits up to 30 s for the games, then sends it anyway |

**Only the newest waits.** The sender keeps the newest document and the newest state of each game (its PGN and its
moves), one request in flight per thread. A request that fails (nyayo is away) is tried again with whatever is newest
by then, and when nyayo answers slower than moves come, intermediate states are skipped, not queued. A thread waits
before trying again as long as nyayo's `Retry-After` says (a `503` when it is busy, up to 60 s), else 1 s doubling to
15 s. Games go once nyayo has answered a first document; a game answered `404` (nyayo doesn't know the match) is kept,
and the last document goes again first. A refusal (`409` for a sealed game, `400`) is logged once, per thread, and not
retried.

**The schedule by changes.** The first report is whole. `report.seq` counts the run's reports from 1, each request a
number. Once an answer has carried `Nyayo-Seq`, a report has `base`, the seq nyayo last acknowledged, and `games`
holds only the entries that changed since then, each whole; the counts, state and the rest stay whole. The report is
planned by the sender when it sends (`changes.py`), against what nyayo acknowledged and not against the last one
built, because a document replaced in the queue never reached nyayo. The digest of the schedule (XOR of each entry's
SHA-256, kept incrementally) is compared with `Nyayo-Schedule-Digest` on every answer. The next report is whole after
a `409`, a failed request, an answer without `Nyayo-Seq` or with another seq or digest than ours, and a game's `404`;
the run's last document and `uwanja report` are whole. So a nyayo that doesn't know `report` is only sent whole
documents. `match.json` in the run directory is always the whole document.

**Moves by changes.** A live game's first send is the whole PGN. Once an answer has carried `Nyayo-Plies`, the moves
after that ply go as `PATCH {"from": n, "moves": [{"san", "comment"}]}`, the comment as in the PGN without the braces,
all the moves made since in one request. The whole PGN goes instead after a `409` or `400` (in the same step, no
waiting), after a failed request, when the game has fewer plies than nyayo holds or its last acknowledged move is not
the same, for the sealed game and from `uwanja report`.

`{n}` is fastchess's game number, the schedule entry's `number`. A live game waits to be sent until the log has said who
plays white and black (a few milliseconds), so its number is stable from the first request.

Counts are from the subject's side: wins, draws, losses, the pentanomial (`LL LD DD DW WW`, from the games of one
fastchess round against one opponent; null when a game had no round), and per side time losses, crashes and illegal
moves. A fault is read from fastchess's reason (`White loses on time`, `Black makes an illegal move`, `White
disconnects`); the side named is at fault, the loser when none is. The document also carries the producer
(`uwanja` and its version, 0.2.0), the subject and each opponent (`id name` split into name and version, commit,
bench, options, rating), settings (TC, book and its SHA-256, fastchess's version, concurrency, adjudication, host) and
`progress` with `games`.

### Move comments

fastchess's `-pgnout` is given `pgn_options` as `nodes=true nps=true pv=true`, and a game in progress is rebuilt from
the log with the same comments, so a live PGN and the finished one read alike. A move's comment is the mover's score
and depth, the time, then the options in fastchess's order (`n`, `sd`, `nps`, `hashfull`, `tbhits`, `pv`); a game's
last move ends with how it ended:

```
2. Nf3 {+0.28/11 0.116s, n=278950, nps=0, pv="g1f3 d7d5 b1c3 b8c6 e4d5 e6d5 d2d4"}
d5 {-0.28/10 0.089s, n=213535, nps=0, pv="d7d5 b1c3 d5e4 c3e4"}
89. Rh8# {+M1/1 0.000s, n=19, nps=0, pv="h2h8", White mates} 1-0
```

- The score is in pawns with a sign, a zero is `0.00`; a mate is `+M<plies>` / `-M<plies>`: the engine's `mate 5` is
  `+M9`, `mate -4` is `-M8` (2n-1 to win, 2n to lose). No score at all is `/0 0.116s`.
- `nps`, `sd`, `hashfull` and `tbhits` are what the engine reported and 0 when it didn't (Elephant reports none of
  them, so `nps=0`).
- `pv` is UCI moves in quotes, space separated, whatever the PGN's move notation; `n` is the last `info`'s nodes.
- A book move is `{book}`. `tl` (timeleft) and `latency`, which fastchess can also write, aren't supported: the log
  doesn't carry them.
- The time in a live PGN is measured from the log's timestamps, within a millisecond of fastchess's own.

The moves a `PATCH` carries are the live PGN's own: the SAN and this comment (`pgnout.live_moves`).

### The run directory

| | |
|:--|:--|
| `gauntlet.toml` | the file the run started from |
| `identity.json` | what's fixed: engines as probed, settings, host, match id |
| `match.json` | the latest document, whole |
| `games/<n>.pgn` | each game's latest PGN, final once it ended |
| `games.pgn`, `fastchess.log`, `fastchess.out`, `fastchess-config.json` | fastchess's own |

### Code

| | |
|:--|:--|
| `cli.py`, `session.py` | commands; one run: the loop, the callbacks, the state |
| `config.py`, `document.py`, `schedule.py`, `results.py`, `rebuild.py` | the file, the match document, the games before they're played and where a finished one goes, counts, the rebuild |
| `hostinfo.py`, `assets.py`, `fetch.py`, `engines.py`, `fastchess.py` | host and CPU, picking an asset, downloading, probing engines, fastchess' command line |
| `events.py`, `uci.py`, `tracker.py`, `pgnout.py`, `replay.py` | log lines to UCI events to games, a game as PGN and as moves, a recorded log replayed |
| `nyayo.py`, `changes.py` | the two sender threads and nyayo's URL and token; which schedule entries a report carries, and the digest |

`tests/` runs the live path against recorded gauntlets in `tests/data` (committed, though `*.log` is ignored
elsewhere): `fc.log` and `fc.pgn`, 16 games of two engines, and `fc-opts.log` and `fc-opts.pgn`, a real gauntlet
recorded with `nodes`, `nps` and `pv` on, against which every move of the live PGNs is compared with fastchess's own.
The sender is tested against stand-ins for nyayo that keep the match schema's rules (`test_changes.py`,
`test_moves.py`, `test_nyayo.py`).

## Interfaces

| Direction | What | Contract |
|:--|:--|:--|
| in | the gauntlet file | above, uwanja's own input format |
| in | fastchess' log, PGN and stdout | fastchess's formats, `v1.8.2-alpha`; the stdout lines and the log's `[Engine]` lines are parsed by regex, they break if fastchess rewords them |
| in | GitHub releases API and downloads | asset names as publishers spell them, `asset` and `binary` override |
| in | the engines' UCI, `buildinfo`, `bench` | elephant's [engine spec](https://github.com/looak/elephant/blob/main/docs/spec/engine.md#uci); other engines answer `uci` and nothing else |
| in | nyayo's answers | the headers `Nyayo-Seq`, `Nyayo-Schedule-Digest` and `Nyayo-Plies`, and `Retry-After` on a `503`, as in the match schema |
| out | the match document | `PUT /api/matches/{id}`, nyayo's [match schema](https://github.com/looak/nyayo/blob/main/docs/match-schema.md), nyayo owns it |
| out | games | `PUT` and `PATCH /api/matches/{id}/games/{n}`, the same |
| out | `NYAYO_URL`, `NYAYO_TOKEN` | as `results.py` takes them |
| out | the run directory | above, read back by `uwanja report` and `uwanja rebuild` |
