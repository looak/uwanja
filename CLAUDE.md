# uwanja

An engine-agnostic gauntlet runner: fetches UCI engines and fastchess, plays a gauntlet, streams games and the match
to nyayo. Python 3.11+, stdlib plus python-chess, and it must stay OS-agnostic (Windows, Linux, macOS): paths through
`pathlib`, no shell, no POSIX-only calls, anything platform specific in `hostinfo.py` with the others' fallbacks.
Sibling checkouts: `../ramani` (direction and plans), `../nyayo` (the server), `../bwawa` (where the tracker came from,
being retired), `../elephant` (an engine this runs, but the runner must never know about it).

## Specs

What this repo does is specified here, in `docs/spec.md`: Purpose, Scope, Current and Interfaces. **Keep it true in
the same pull request as the code**: a change that alters a command, an endpoint, a schema, a file format or anything
the spec describes updates the spec in that pull request. When the code and the spec disagree, the code wins and the
spec is fixed.

Where this repo is going lives in ramani (github.com/looak/ramani, private, sibling checkout at `../ramani`): the
README for the system and the contracts table, `uwanja.md` for this repo's Direction (planned work, open
decisions), and `plans/` for work spanning repos. **Read ramani, never edit it**, nor any other sibling checkout. A
pull request that does a plan's step says `Closes looak/<repo>#<sub-issue>`. When it settles one of ramani's open
decisions, say so in the final report. This repo owns no contract of its own: it sends nyayo's match schema (nyayo's
`docs/match-schema.md`, changed only there, by agreement with the owner), and the gauntlet file is its own input
format, described in `docs/spec.md`.

## Working here

- `python -m venv .venv`, then `.venv/Scripts/pip install -r requirements.txt` and `pip install pytest`
  (`.venv/bin/pip` on Linux); `pytest` runs the suite. The live path is tested against a recorded gauntlet in `tests/data`; `uwanja follow` and `uwanja replay`
  are how to develop it without playing one.
- Config keys live in the tables at the top of `config.py`; asset and CPU spellings in `assets.py` and
  `hostinfo.TIERS` are data, a new engine's naming is a line there.
- The runner computes no statistics and validates nothing fastchess owns (time controls, books). It sends counts.
- A report is the whole truth so far, or a change on top of what nyayo has acknowledged. A lost, refused or doubtful
  request is never repaired by guessing: the next one is whole. Changes follow an answer that carried `Nyayo-Seq` or
  `Nyayo-Plies`; keep it that way.
