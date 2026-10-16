import pytest

from uwanja.assets import NoAsset, choose_asset
from uwanja.hostinfo import HostInfo

FASTCHESS = ["fastchess-linux-arm64.tar", "fastchess-linux-risc-v.tar", "fastchess-linux-x86-64.tar",
             "fastchess-mac-arm64.tar", "fastchess-mac-x86-64.tar", "fastchess-windows-arm64.zip",
             "fastchess-windows-x86-64.zip"]

STOCKFISH = ["stockfish-ubuntu-x86-64.tar", "stockfish-ubuntu-x86-64-avx2.tar", "stockfish-ubuntu-x86-64-bmi2.tar",
             "stockfish-ubuntu-x86-64-avx512.tar", "stockfish-ubuntu-x86-64-vnni512.tar",
             "stockfish-ubuntu-x86-64-sse41-popcnt.tar", "stockfish-windows-x86-64-avx2.zip",
             "stockfish-windows-x86-64-bmi2.zip", "stockfish-macos-m1-apple-silicon.tar", "sha256sums.txt"]


def host(os_="windows", arch="x86_64", flags=(), **kwargs):
    return HostInfo(os_, arch, flags=frozenset(flags), **kwargs)


def test_fastchess_by_os_and_arch():
    assert choose_asset(FASTCHESS, host("windows", "x86_64")) == "fastchess-windows-x86-64.zip"
    assert choose_asset(FASTCHESS, host("windows", "arm64")) == "fastchess-windows-arm64.zip"
    assert choose_asset(FASTCHESS, host("linux", "x86_64")) == "fastchess-linux-x86-64.tar"
    assert choose_asset(FASTCHESS, host("macos", "arm64")) == "fastchess-mac-arm64.tar"
    assert choose_asset(FASTCHESS, host("macos", "x86_64")) == "fastchess-mac-x86-64.tar"


def test_cpu_tier_picks_the_best_the_host_runs():
    avx2 = {"avx2", "bmi2", "sse4_1", "popcnt"}
    assert choose_asset(STOCKFISH, host("linux", flags=avx2)) == "stockfish-ubuntu-x86-64-bmi2.tar"
    assert choose_asset(STOCKFISH, host("linux", flags={"avx2", "sse4_1", "popcnt"})) == "stockfish-ubuntu-x86-64-avx2.tar"
    assert choose_asset(STOCKFISH, host("linux", flags={"sse4_1", "popcnt"})) == "stockfish-ubuntu-x86-64-sse41-popcnt.tar"
    assert choose_asset(STOCKFISH, host("linux")) == "stockfish-ubuntu-x86-64.tar"
    full = avx2 | {"avx512f", "avx512bw", "avx512vl"}
    assert choose_asset(STOCKFISH, host("linux", flags=full)) == "stockfish-ubuntu-x86-64-avx512.tar"
    assert choose_asset(STOCKFISH, host("linux", flags=full | {"avx512vnni"})) == "stockfish-ubuntu-x86-64-vnni512.tar"


def test_slow_pext_prefers_avx2_over_bmi2():
    zen2 = host("linux", flags={"avx2", "bmi2", "sse4_1", "popcnt"}, slow_pext=True)
    assert choose_asset(STOCKFISH, zen2) == "stockfish-ubuntu-x86-64-avx2.tar"


def test_override_glob_and_errors(monkeypatch):
    assert choose_asset(STOCKFISH, host("linux"), "*vnni512*") == "stockfish-ubuntu-x86-64-vnni512.tar"
    with pytest.raises(NoAsset):
        choose_asset(STOCKFISH, host("linux"), "*nothing*")
    with pytest.raises(NoAsset):
        choose_asset(["engine-linux-x86-64"], host("windows"))
    monkeypatch.setenv("UWANJA_CPU", "avx2")
    full = {"avx2", "bmi2", "sse4_1", "popcnt", "avx512f", "avx512bw", "avx512vl"}
    assert choose_asset(STOCKFISH, host("linux", flags=full)) == "stockfish-ubuntu-x86-64-avx2.tar"


def test_bare_executables_and_checksums_ignored():
    names = ["Engine-1.0-win64.exe", "Engine-1.0-linux", "Engine-1.0-win64.exe.sha256", "Source code (zip)"]
    assert choose_asset(names, host("windows")) == "Engine-1.0-win64.exe"
    assert choose_asset(names, host("linux")) == "Engine-1.0-linux"


def test_android_builds_are_not_linux():
    names = ["engine-android-arm64-universal.tar.gz", "engine-linux-arm64-universal.tar.gz"]
    assert choose_asset(names, host("linux", "arm64")) == "engine-linux-arm64-universal.tar.gz"
    with pytest.raises(NoAsset):
        choose_asset(names[:1], host("linux", "arm64"))


def test_cpuinfo_flags_become_tiers():
    from uwanja.hostinfo import parse_cpuinfo
    info = {"brand_raw": "AMD Ryzen 9 5950X", "vendor_id_raw": "AuthenticAMD", "family": 25,
            "flags": ["sse4_1", "sse4_2", "popcnt", "avx", "avx2", "bmi1", "bmi2", "avx512f", "avx512bw",
                      "avx512vl", "avx512_vnni"]}
    flags, name, vendor, family = parse_cpuinfo(info)
    assert {"avx2", "bmi2", "avx512vnni", "popcnt"} <= flags and family == 25
    zen5 = HostInfo("linux", "x86_64", flags=frozenset(flags))
    assert [tier for tier, _ in zen5.tiers()][:2] == ["vnni512", "avx512"]
    assert choose_asset(STOCKFISH, zen5) == "stockfish-ubuntu-x86-64-vnni512.tar"
    assert choose_asset(STOCKFISH, HostInfo("linux", "x86_64", flags=frozenset(flags - {"avx512vnni"}))) \
        == "stockfish-ubuntu-x86-64-avx512.tar"


ELEPHANT = ["elephant-gambit-1.0.0-linux-x86-64", "elephant-gambit-1.0.0-linux-x86-64-v2",
            "elephant-gambit-1.0.0-linux-x86-64-v3", "elephant-gambit-1.0.0-windows-x86-64-v2.exe",
            "elephant-gambit-1.0.0-windows-x86-64-v3.exe", "elephant-gambit-1.0.0-windows-x86-64.exe"]


def test_microarchitecture_levels_as_elephants_releases_name_them():
    v3 = {"avx2", "bmi1", "bmi2", "fma", "sse4_1", "sse4_2", "ssse3", "popcnt"}
    assert choose_asset(ELEPHANT, host("windows", flags=v3)) == "elephant-gambit-1.0.0-windows-x86-64-v3.exe"
    assert choose_asset(ELEPHANT, host("linux", flags=v3)) == "elephant-gambit-1.0.0-linux-x86-64-v3"
    v2 = {"sse4_1", "sse4_2", "ssse3", "popcnt"}
    assert choose_asset(ELEPHANT, host("linux", flags=v2)) == "elephant-gambit-1.0.0-linux-x86-64-v2"
    assert choose_asset(ELEPHANT, host("linux")) == "elephant-gambit-1.0.0-linux-x86-64"
    v4 = v3 | {"avx512f", "avx512bw", "avx512cd", "avx512dq", "avx512vl"}
    assert choose_asset(ELEPHANT, host("windows", flags=v4)) == "elephant-gambit-1.0.0-windows-x86-64-v3.exe"
