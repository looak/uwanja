"""Which file of a release is the one for this host.

Release assets are named by whoever cuts the release, `fastchess-windows-x86-64.zip`, `engine-linux-x86-64-avx2`,
`Engine_v2_win64_bmi2.exe`. The picker reads three things from a name, the OS, the architecture and a CPU tier (see
hostinfo.TIERS), drops assets for another OS, architecture or a tier the host can't run, and takes the one the host
prefers. The word tables are data on purpose, an engine with its own spelling is one line here, or `asset = "<glob>"`
in the config for that engine alone.
"""
from __future__ import annotations

import fnmatch
import re

from .hostinfo import TIERS, HostInfo

OS_WORDS = {
    "windows": ("windows", "win64", "win32", "mingw"),
    "linux": ("linux", "ubuntu"),
    "android": ("android",),       # never a host, only keeps its builds from passing as Linux ones
    "macos": ("macos", "mac-", "-mac", "_mac", "osx", "darwin", "apple"),
}
ARCH_WORDS = {
    "x86_64": ("x86-64", "x86_64", "x64", "amd64", "win64"),
    "arm64": ("arm64", "aarch64"),
}
# what a release carries besides binaries, never what's wanted
IGNORED = re.compile(r"(\.sha\d*|\.md5|\.sig|\.asc|\.txt|\.md|\.json|\.pdf|\.nnue|\.deb|\.rpm|\.msi|\.7z|\.dmg|\.pkg)$"
                     r"|source|(^|[-_.])src([-_.]|$)|checksum|sha256", re.IGNORECASE)


class NoAsset(Exception):
    pass


def _norm(name: str) -> str:
    return name.lower().replace("_", "-")


def _os_of(name: str) -> str | None:
    if name.endswith(".exe"):
        return "windows"
    for os_, words in OS_WORDS.items():
        if any(_norm(word) in name for word in words):
            return os_
    return None


def _arch_of(name: str) -> str | None:
    for arch, words in ARCH_WORDS.items():
        if any(_norm(word) in name for word in words):
            return arch
    return None


def _tier_of(name: str, arch: str) -> str | None:
    """The most demanding tier the name mentions."""
    for tier, aliases, _ in TIERS.get(arch, []):
        # `x86-64-v3`, not `x86-64-v30`: a level is a whole token
        if any(re.search(rf"(?<![0-9a-z]){re.escape(_norm(alias))}(?![0-9a-z])", name) for alias in aliases):
            return tier
    return None


def choose_asset(names: list[str], host: HostInfo, pattern: str | None = None) -> str:
    """The name in `names` for this host. `pattern` is a glob picking the asset outright (first match, by name)."""
    if pattern:
        for name in names:
            if fnmatch.fnmatch(name.lower(), pattern.lower()):
                return name
        raise NoAsset(f"no asset matches asset = {pattern!r} in: {', '.join(names) or 'none'}")

    tiers = [name for name, _ in host.tiers()]
    ranked: list[tuple[tuple, str]] = []
    for name in names:
        if IGNORED.search(name):
            continue
        low = _norm(name)
        os_, arch = _os_of(low), _arch_of(low)
        if os_ is not None and os_ != host.os:
            continue
        if arch is not None and arch != host.arch:
            continue
        tier = _tier_of(low, host.arch)
        if tier is not None and tier not in tiers:
            continue
        ranked.append(((os_ == host.os, arch == host.arch,
                        len(tiers) - tiers.index(tier) if tier else 0,     # a build for the tier beats a generic one
                        -len(name)), name))
    if not ranked:
        raise NoAsset(f"no asset for {host.os} {host.arch} among: {', '.join(names) or 'none'} "
                      "(asset = \"<glob>\" in the config picks one by hand)")
    ranked.sort(key=lambda entry: entry[0], reverse=True)
    # an asset that names neither OS nor architecture only wins when nothing names the host
    return ranked[0][1]
