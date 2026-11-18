"""What the match document says about where each engine came from."""
from pathlib import Path

from uwanja.config import parse
from uwanja.document import Identity
from uwanja.engines import Resolved
from uwanja.hostinfo import HostInfo


def identity():
    gauntlet = parse({
        "name": "g", "tc": "1+0.01", "subject": {"url": "https://github.com/looak/elephant", "binary": "elephant-*"},
        "opponent": [
            {"url": "https://github.com/o/Barbarossa", "asset": "*linux*", "binary": "*/barb",
             "rating": {"value": 2465, "list": "CCRL Blitz", "source": "https://computerchess.org.uk/ccrl/404/b"}},
            {"url": "https://github.com/o/Plain", "rating": 2000},
            {"cmd": "engines/local"},
        ]})
    release = lambda repo, tag: {"kind": "release", "url": f"https://github.com/{repo}", "tag": tag, "asset": "a.zip"}
    origins = [release("looak/elephant", "1.4.0"), release("o/Barbarossa", "v0.6.0"), release("o/Plain", "2.0"),
               {"kind": "local"}]
    engines = [Resolved(spec, Path(spec.cmd or "x"), Path("."), origin) for spec, origin in zip(gauntlet.engines, origins)]
    return Identity("m", gauntlet, engines[0], engines[1:], HostInfo("linux", "x86_64"), "fc")


def test_a_fetched_engine_carries_its_repository_and_the_tag_as_resolved():
    who = identity()
    assert who.subject["url"] == who.subject["repo"] == "https://github.com/looak/elephant"
    assert who.subject["ref"] == "1.4.0" and who.subject["binary"] == "elephant-*" and "asset" not in who.subject
    barbarossa, plain, _ = who.opponents
    assert (barbarossa["url"], barbarossa["ref"]) == ("https://github.com/o/Barbarossa", "v0.6.0")
    assert (barbarossa["asset"], barbarossa["binary"]) == ("*linux*", "*/barb")
    assert plain["ref"] == "2.0" and "asset" not in plain and "binary" not in plain


def test_a_local_engine_carries_neither():
    local = identity().opponents[2]
    assert "url" not in local and "ref" not in local


def test_the_rating_source_is_in_the_document():
    barbarossa, plain, _ = identity().opponents
    assert barbarossa["rating"]["source"] == "https://computerchess.org.uk/ccrl/404/b"
    assert "source" not in plain["rating"]
