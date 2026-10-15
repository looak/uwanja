# uwanja

*Swahili for a playing field, where the herd meets others.*

An engine-agnostic gauntlet runner. Give it a list of UCI engines, it fetches them (release binaries, or built from
a GitHub repo), plays a gauntlet through [fastchess](https://github.com/Disservin/fastchess), streams every game to
[nyayo](https://github.com/looak/nyayo) while it's played, and sends a match report at the end.

Status: planning. The design is in ramani's `plans/gauntlet-runner.md`.

## Configuration (sketch)

```toml
tc = "120+1"
book = "UHO_Lichess_4852_v1.epd"
rounds = 100

[[engine]]            # the first entry is the engine under test
url = "https://github.com/looak/elephant"
ref = "1.0.0"
build = "make -C ob_build EXE=elephant"

[[engine]]            # everything else is usually a release binary
url = "https://github.com/<owner>/<engine>/releases/tag/<tag>"
rating = 2465
```

Release assets are picked by OS and CPU level, with `asset = "<glob>"` to override. Engines and fastchess are cached
by URL and commit.
