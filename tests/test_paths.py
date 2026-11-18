"""Every path handed to fastchess is absolute, whatever the gauntlet file's directory, the cwd and --out are."""
import json
import sys
from pathlib import Path

from uwanja import cli, fastchess as fastchess_mod
from uwanja.document import Identity
from uwanja.engines import Resolved
from uwanja.fastchess import Fastchess
from uwanja.hostinfo import HostInfo
from uwanja.config import load
from uwanja.resume import play_block
from uwanja.session import RunPaths

TOML = '''name = "sub"
tc = "1+0"
book = "../books/b.epd"
rounds = 2
[subject]
cmd = "{exe}"
[[opponent]]
cmd = "{exe}"
id = "o"
'''


def tokens(argv):
    """The values fastchess reads as files: after file=, outname=, dir=, cmd=."""
    found = []
    for token in argv:
        for key in ("file=", "outname=", "dir=", "cmd="):
            if token.startswith(key):
                found.append(token[len(key):])
    return found


def layout(tmp_path):
    (tmp_path / "gauntlets").mkdir()
    (tmp_path / "books").mkdir()
    (tmp_path / "books" / "b.epd").write_text("8/8/8/8/8/8/8/K1k5 w - - 0 1\n8/8/8/8/8/8/8/K1k5 b - - 0 1\n")
    file = tmp_path / "gauntlets" / "g.toml"
    file.write_text(TOML.format(exe=Path(sys.executable).as_posix()))
    return file


def test_a_relative_out_with_the_file_in_a_subdirectory(tmp_path, monkeypatch, capsys):
    file = layout(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(fastchess_mod, "locate", lambda g, h, log=print: Fastchess(Path(sys.executable), "1"))
    monkeypatch.setattr(cli, "_engines", lambda g, h: [
        Resolved(spec, Path(sys.executable), Path(sys.executable).parent, {"kind": "local"}) for spec in g.engines])
    assert cli.main(["run", str(file.relative_to(tmp_path)), "--dry-run", "--out", "runs"]) == 0
    line = capsys.readouterr().out.strip().splitlines()[-1]
    argv = line.replace('"', "").split(" ")
    openings = [t for t in tokens(argv) if t.endswith("openings.epd")]
    assert len(openings) == 1
    for path in tokens(argv):
        assert Path(path).is_absolute(), path
    assert Path(openings[0]).parent == (tmp_path / "runs" / Path(openings[0]).parent.name).resolve()
    assert "gauntlets" not in openings[0]


def test_a_resume_from_a_relative_run_directory(tmp_path, monkeypatch, capsys):
    file = layout(tmp_path)
    gauntlet = load(file)
    engines = [Resolved(spec, Path(sys.executable), Path(sys.executable).parent, {"kind": "local"}, spec.id)
               for spec in gauntlet.engines]
    identity = Identity("m", gauntlet, engines[0], engines[1:], HostInfo("windows", "x86_64"), "x")
    identity.play = play_block(Fastchess(Path(sys.executable), "1"), engines, 1, True)
    run = tmp_path / "runs" / "r"
    run.mkdir(parents=True)
    (run / "gauntlet.toml").write_text(file.read_text())
    (run / "identity.json").write_text(json.dumps(identity.to_json()))
    (run / "openings.epd").write_text("8/8/8/8/8/8/8/K1k5 w - - 0 1\n8/8/8/8/8/8/8/K1k5 b - - 0 1\n")
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(tmp_path)
    assert cli.main(["run", "--resume", "runs/r", "--dry-run", "--no-report"]) == 0
    line = capsys.readouterr().out.strip().splitlines()[-1]
    argv = line.replace('"', "").split(" ")
    assert tokens(argv)
    for path in tokens(argv):
        assert Path(path).is_absolute(), path
    assert Path(RunPaths(Path("runs/r")).root) == run.resolve()
