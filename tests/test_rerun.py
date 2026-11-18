"""The config in every document, and a new match from a stored one."""
import html
import json
import sys
import tomllib
from pathlib import Path

import pytest

from uwanja import rerun
from uwanja.config import parse
from uwanja.document import Identity
from uwanja.engines import Resolved
from uwanja.fastchess import Fastchess
from uwanja.hostinfo import HostInfo
from uwanja.results import Results

RAW = {
    "name": "g", "tc": "8+0.08", "rounds": 3, "concurrency": 4, "book": "../books/UHO.epd",
    "adjudication": {"win": "movecount=3 score=400"},
    "subject": {"url": "https://github.com/looak/elephant", "ref": "1.2.0", "options": {"Threads": 1, "Hash": 64}},
    "opponent": [{"url": "https://github.com/o/eng", "ref": "v1", "binary": "*/eng", "id": "eng",
                  "rating": {"value": 2700, "list": "CCRL Blitz", "as_of": "2026-10-18"}},
                 {"url": "https://github.com/o/two", "ref": "v2"}],
}


def built(tmp_path, raw=RAW):
    book = tmp_path / "books" / "UHO.epd"
    book.parent.mkdir(exist_ok=True)
    book.write_text("8/8/8/8/8/8/8/K1k5 w - - 0 1\n")
    gauntlet = parse(raw | ({"book": str(book)} if "book" in raw else {}), tmp_path)
    engines = []
    for spec in gauntlet.engines:
        exe = tmp_path / f"{spec.id}.exe"
        exe.write_bytes(spec.id.encode())
        origin = {"kind": "release", "url": spec.url, "tag": spec.ref or "latest", "asset": f"{spec.id}.zip",
                  "published_at": "2026-01-01T00:00:00Z"} if spec.url else {"kind": "local"}
        engines.append(Resolved(spec, exe, tmp_path, origin, f"{spec.id} 1.0"))
    runner = Fastchess(Path(sys.executable), "v1.8.2-alpha")
    return gauntlet, engines, runner


def test_config_holds_the_gauntlet_as_asked_with_defaults_and_what_it_became(tmp_path):
    gauntlet, engines, runner = built(tmp_path)
    config = rerun.config_block(gauntlet, runner, engines)
    assert config["format"] == "uwanja.gauntlet/1" and config["portable"] is True
    asked = config["gauntlet"]
    assert asked["book"] == "UHO.epd" and asked["book_format"] == "epd" and asked["book_order"] == "random"
    assert asked["recover"] is True and asked["fastchess"] == {"version": "v1.8.2-alpha"}
    assert asked["subject"]["ref"] == "1.2.0" and asked["opponent"][0]["rating"]["value"] == 2700
    resolved = config["resolved"]
    assert resolved["book"]["name"] == "UHO.epd" and len(resolved["book"]["sha256"]) == 64
    assert resolved["opponents"][0]["tag"] == "v1" and len(resolved["opponents"][0]["sha256"]) == 64
    assert resolved["runner"]["version"] == "v1.8.2-alpha" and len(resolved["runner"]["sha256"]) == 64
    assert len(json.dumps(config)) < 64 * 1024


def test_the_stored_gauntlet_is_a_gauntlet_file(tmp_path):
    gauntlet, engines, runner = built(tmp_path)
    again = rerun.gauntlet_json(gauntlet) | {"book": str(tmp_path / "books" / "UHO.epd")}
    other = parse(again, tmp_path)
    assert rerun.gauntlet_json(other) == rerun.gauntlet_json(gauntlet)
    assert [e.id for e in other.engines] == [e.id for e in gauntlet.engines]


def test_a_local_command_or_path_makes_it_not_portable_and_nothing_secret_is_in_it(tmp_path, monkeypatch):
    monkeypatch.setenv("NYAYO_TOKEN", "sekrit-token")
    monkeypatch.setenv("NYAYO_URL", "https://nyayo.example")
    raw = RAW | {"subject": {"cmd": "../engines/elephant", "options": {"Hash": 64}}}
    gauntlet, engines, runner = built(tmp_path, raw)
    config = rerun.config_block(gauntlet, runner, engines)
    assert config["portable"] is False
    text = json.dumps(config)
    assert "sekrit-token" not in text and "nyayo.example" not in text and str(tmp_path) not in text


def test_every_document_carries_it_and_a_run_directory_keeps_it(tmp_path):
    gauntlet, engines, runner = built(tmp_path)
    identity = Identity("m", gauntlet, engines[0], engines[1:], HostInfo("windows", "x86_64"), "x")
    results = Results(identity.subject_id, [o["id"] for o in identity.opponents])
    assert "config" not in identity.document(results, "running")
    identity.config = rerun.config_block(gauntlet, runner, engines)
    again = Identity.from_json(json.loads(json.dumps(identity.to_json())))
    for state in ("running", "stopped", "finished"):
        assert again.document(results, state)["config"] == identity.config


def page(toml_text):
    field = html.escape(toml_text)
    return f'<html><textarea id="copy-match-config" class="copy-field mono" rows="18" readonly>{field}</textarea></html>'


FILE = '''name = "g"
tc = "8+0.08"
book = "UHO.epd"
rounds = 3
concurrency = 4

[subject]
url = "https://github.com/looak/elephant"
ref = "1.2.0"
options = { Threads = 1, Hash = 64 }

[[opponent]]  # it's "quoted" <b>
url = "https://github.com/o/eng"
id = "eng"
rating = { value = 2700, list = "CCRL Blitz" }
'''


def test_a_match_page_gives_the_gauntlet_to_play_again(tmp_path):
    (tmp_path / "books").mkdir()
    (tmp_path / "books" / "UHO.epd").write_text("x\n")
    gauntlet, text = rerun.gauntlet_from_page(page(FILE), tmp_path)
    assert gauntlet.name == "g" and gauntlet.rounds == 3 and gauntlet.subject.ref == "1.2.0"
    assert gauntlet.opponents[0].rating.value == 2700
    assert Path(gauntlet.book) == (tmp_path / "books" / "UHO.epd").resolve()
    assert tomllib.loads(text)["name"] == "g"


def test_a_page_without_config_or_without_the_book_says_so(tmp_path):
    with pytest.raises(rerun.RerunError, match="no `config`"):
        rerun.gauntlet_from_page("<html>a match</html>", tmp_path)
    with pytest.raises(rerun.RerunError, match="UHO.epd"):
        rerun.gauntlet_from_page(page(FILE), tmp_path)


def test_a_match_id_is_taken_on_the_configured_nyayo():
    assert rerun.match_url("https://n.example/matches/abc", None) == "https://n.example/matches/abc"
    assert rerun.match_url("abc", "https://n.example/") == "https://n.example/matches/abc"
    with pytest.raises(rerun.RerunError):
        rerun.match_url("abc", None)


def test_the_token_goes_only_to_the_nyayo_it_belongs_to(monkeypatch):
    seen = []

    class Answer:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b"ok"

    monkeypatch.setattr(rerun.urllib.request, "urlopen", lambda request, timeout: seen.append(request) or Answer())
    rerun.fetch_page("https://n.example/matches/a", "tok", "https://n.example")
    rerun.fetch_page("https://other.example/matches/a", "tok", "https://n.example")
    assert seen[0].get_header("Authorization") == "Bearer tok" and seen[1].get_header("Authorization") is None
