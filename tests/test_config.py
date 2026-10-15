import pytest

from uwanja.config import ConfigError, parse

BASE = {
    "name": "g", "tc": "120+1", "book": "b.epd", "rounds": 100, "concurrency": 14,
    "subject": {"url": "https://github.com/looak/elephant", "ref": "1.0.0", "options": {"Threads": 1, "Hash": 64}},
    "opponent": [
        {"url": "https://github.com/o/Barbarossa/releases/tag/v0.6.0",
         "rating": {"value": 2465, "list": "CCRL Blitz", "as_of": "2026-10-01"}},
        {"cmd": "engines/other.exe", "rating": 2300},
    ],
}


def test_parses_the_plans_shape():
    gauntlet = parse(BASE)
    assert [engine.id for engine in gauntlet.engines] == ["elephant", "barbarossa", "other"]
    assert gauntlet.subject.options == {"Threads": 1, "Hash": 64}
    assert gauntlet.opponents[0].rating.list == "CCRL Blitz"
    assert gauntlet.opponents[1].rating.value == 2300 and gauntlet.opponents[1].rating.list is None


def test_duplicate_ids_get_a_suffix():
    raw = {**BASE, "opponent": [{"url": "https://github.com/a/elephant"}, {"url": "https://github.com/b/elephant"}]}
    assert [engine.id for engine in parse(raw).engines] == ["elephant", "elephant-2", "elephant-3"]


@pytest.mark.parametrize("change, message", [
    ({"tcc": "1+1"}, "unknown 'tcc'"),
    ({"subject": {"url": "u", "cmd": "c"}}, "exactly one of url"),
    ({"subject": {"url": "u", "rating": 1}}, "no rating"),
    ({"opponent": []}, "opponent"),
    ({"opponent": [{"cmd": "x", "colour": "red"}]}, "unknown 'colour'"),
    ({"rounds": 0}, "whole numbers"),
])
def test_rejects(change, message):
    with pytest.raises(ConfigError, match=message):
        parse({**BASE, **change})
