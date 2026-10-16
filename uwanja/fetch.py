"""Getting binaries: GitHub releases, downloaded once and cached, unpacked, the executable found.

stdlib only. Anonymous GitHub API calls are rate limited (60 an hour); `GITHUB_TOKEN` (or `GH_TOKEN`) lifts that and
reaches private repos. The cache is `$UWANJA_HOME`, else the OS's cache directory.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .assets import choose_asset
from .hostinfo import HostInfo

API = "https://api.github.com"
_REPO_URL = re.compile(r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/#?]+?)(?:\.git)?"
                       r"(?:/releases/tag/(?P<tag>[^/#?]+)|/tree/(?P<branch>[^#?]+))?/?$")


class FetchError(Exception):
    pass


def cache_dir() -> Path:
    if os.environ.get("UWANJA_HOME"):
        return Path(os.environ["UWANJA_HOME"]) / "cache"
    if sys.platform == "win32":
        if "WindowsApps" in sys.base_prefix:
            # Python from the Microsoft Store virtualises AppData: files written there are real to Python but
            # other programs see a different path, and .NET single-file engines can't resolve their own (Lynx).
            return Path.home() / ".uwanja" / "cache"
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "uwanja" / "cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "uwanja"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "uwanja"


def _token() -> str | None:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def _request(url: str, accept: str = "application/vnd.github+json") -> urllib.request.Request:
    headers = {"User-Agent": "uwanja", "Accept": accept}
    if _token() and url.startswith(API):
        headers["Authorization"] = f"Bearer {_token()}"
    return urllib.request.Request(url, headers=headers)


def github_json(path: str) -> dict:
    try:
        with urllib.request.urlopen(_request(f"{API}{path}"), timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        hint = " (set GITHUB_TOKEN for private repositories and a higher rate limit)" if error.code in (403, 404) else ""
        raise FetchError(f"GitHub {path}: HTTP {error.code}{hint}") from None
    except OSError as error:
        raise FetchError(f"GitHub {path}: {error}") from None


def parse_repo_url(url: str) -> tuple[str, str, str | None]:
    """(owner, repo, tag) of a GitHub repository or release URL, tag None for a plain repository."""
    found = _REPO_URL.match(url.strip())
    if found is None:
        raise FetchError(f"not a GitHub repository or release URL: {url}")
    return found["owner"], found["repo"], found["tag"]


def find_release(owner: str, repo: str, ref: str | None) -> dict:
    """The release for a tag (also tried with a `v` added or removed), the latest one without."""
    if ref is None or ref == "latest":
        return github_json(f"/repos/{owner}/{repo}/releases/latest")
    candidates = [ref] + ([ref[1:]] if ref.startswith("v") else [f"v{ref}"])
    last: FetchError | None = None
    for tag in candidates:
        try:
            return github_json(f"/repos/{owner}/{repo}/releases/tags/{tag}")
        except FetchError as error:
            last = error
    raise last or FetchError(f"no release {ref} in {owner}/{repo}")


@dataclass
class Fetched:
    exe: Path
    owner: str
    repo: str
    tag: str
    asset: str
    url: str
    published_at: str | None = None


def fetch_release_binary(url: str, ref: str | None, host: HostInfo, *, asset: str | None = None,
                         binary: str | None = None, kind: str = "engines", log=print) -> Fetched:
    """The executable of a release asset, downloaded and unpacked into the cache the first time.

    `url` is a repository or a release URL, `ref` a tag (the release URL's tag when it has one, the latest release
    otherwise), `asset` a glob over asset names and `binary` a glob over paths inside the archive, both for the
    cases the picker gets wrong."""
    owner, repo, url_tag = parse_repo_url(url)
    release = find_release(owner, repo, ref or url_tag)
    tag = release["tag_name"]
    by_name = {entry["name"]: entry for entry in release.get("assets", [])}
    name = choose_asset(list(by_name), host, asset)
    entry = by_name[name]

    root = cache_dir() / kind / owner / repo / f"{tag}-{entry['id']}"
    done = root / ".complete"
    if not done.exists():
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        archive = root / name
        log(f"uwanja: downloading {owner}/{repo} {tag}: {name}")
        _download(entry, archive)
        _unpack(archive, root / "files")
        done.write_text(json.dumps({"asset": name, "tag": tag}), encoding="utf-8")
    exe = find_executable(root / "files" if (root / "files").exists() else root, host, repo, binary, root / name)
    return Fetched(exe, owner, repo, tag, name, url, release.get("published_at"))


def _download(entry: dict, target: Path) -> None:
    # the API URL with a token also works for private repositories, the public URL needs none
    url = entry["url"] if _token() else entry["browser_download_url"]
    try:
        with urllib.request.urlopen(_request(url, "application/octet-stream"), timeout=60) as response, \
                open(target, "wb") as out:
            shutil.copyfileobj(response, out)
    except (urllib.error.URLError, OSError) as error:
        raise FetchError(f"download of {entry['name']}: {error}") from None


def _unpack(archive: Path, into: Path) -> None:
    into.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(into)
    elif name.endswith((".tar", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2")):
        with tarfile.open(archive) as bundle:
            target = into.resolve()
            for member in bundle.getmembers():
                resolved = (into / member.name).resolve()
                if not (resolved == target or target in resolved.parents) or member.issym() or member.islnk():
                    raise FetchError(f"{archive.name}: refusing {member.name}, it points outside the archive")
            bundle.extractall(into)    # members checked above
    else:
        shutil.copy2(archive, into / archive.name)    # a bare executable


_NOT_EXECUTABLE = re.compile(r"(\.(txt|md|json|nnue|dll|so|dylib|pdf|html|ini|cfg|sh|bat|py|zip|tar|gz|xz)$)|^(license|readme|copying|notice)",
                             re.IGNORECASE)


def find_executable(directory: Path, host: HostInfo, prefer: str | None = None, pattern: str | None = None,
                    archive: Path | None = None) -> Path:
    """The engine's executable inside an unpacked release. `pattern` is a glob over the path inside the archive;
    without it, executables by extension (`.exe` on Windows, none elsewhere), the one named after the repository
    first, then the largest."""
    files = [path for path in directory.rglob("*") if path.is_file() and path != archive]
    if pattern:
        matches = [path for path in files
                   if fnmatch.fnmatch(path.relative_to(directory).as_posix().lower(), pattern.lower())
                   or fnmatch.fnmatch(path.name.lower(), pattern.lower())]
        if not matches:
            raise FetchError(f"binary = {pattern!r} matches nothing in the release, it has: "
                             + ", ".join(sorted(path.relative_to(directory).as_posix() for path in files)[:20]))
        chosen = matches[0]
    else:
        if host.os == "windows":
            candidates = [path for path in files if path.suffix.lower() == ".exe"]
        else:
            candidates = [path for path in files if not _NOT_EXECUTABLE.search(path.name)
                          and (path.suffix == "" or os.access(path, os.X_OK)) and path.stat().st_size > 100_000]
        if not candidates:
            raise FetchError(f"no executable in the release among: "
                             + ", ".join(sorted(path.name for path in files)[:20]) + " (binary = \"<glob>\" picks one)")
        key = (prefer or "").lower()
        candidates.sort(key=lambda path: (key in path.name.lower() if key else False, path.stat().st_size), reverse=True)
        chosen = candidates[0]
    if host.os != "windows":
        chosen.chmod(chosen.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return chosen
