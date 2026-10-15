# uwanja

An engine-agnostic gauntlet runner: fetches UCI engines and fastchess, plays a gauntlet, streams games and the match
to nyayo. Python 3.11+, stdlib plus python-chess, and it must stay OS-agnostic (Windows, Linux, macOS): paths through
`pathlib`, no shell, no POSIX-only calls, anything platform specific in `hostinfo.py` with the others' fallbacks.
Sibling checkouts: `../ramani` (specs and plans), `../nyayo` (the server), `../bwawa` (where the tracker came from,
being retired), `../elephant` (an engine this runs, but the runner must never know about it).

## Specs

The spec lives in ramani (`../ramani`, github.com/looak/ramani): `plans/gauntlet-runner.md` for this repo,
`plans/match-schema.md` for the document and the game PUTs it sends (owned by nyayo once it lands),
`plans/retire-bwawa.md` for what moved here. Plans are proposals: where this code and a plan differ, say so and settle
it with the owner rather than quietly choosing. When a change here alters a contract (the match document, the nyayo
routes used, the gauntlet file) or settles an open decision in a plan, update the plan in ramani in the same piece of
work.

## Working here

- `python -m venv .venv`, then `.venv/Scripts/pip install -r requirements.txt` and `pip install pytest`
  (`.venv/bin/pip` on Linux); `pytest` runs the suite. The live path is tested against a recorded gauntlet in `tests/data`; `uwanja follow` and `uwanja replay`
  are how to develop it without playing one.
- Config keys live in the tables at the top of `config.py`; asset and CPU spellings in `assets.py` and
  `hostinfo.TIERS` are data, a new engine's naming is a line there.
- The runner computes no statistics and validates nothing fastchess owns (time controls, books). It sends counts.
- Reports are always "the whole truth so far", so a lost request is never retried as a delta; keep it that way.
