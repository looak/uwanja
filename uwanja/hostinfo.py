"""What the host is: OS, CPU architecture and which instruction set tier it runs best.

Tiers are how release assets name their builds (`x86-64-avx2`, `-bmi2`, ...). They're ordered best first, a build is
a candidate when the host supports its tier, and the first in the list wins. `UWANJA_CPU=<tier>` overrides detection
(the tier and everything below it), for a host that detects wrong or to pick a conservative build.
"""
from __future__ import annotations

import os
import platform
import re
import socket
import subprocess
import sys
from dataclasses import dataclass, field

# (tier, names an asset may use for it, flags the host needs), best first. Tiers rank by what they need, whether the
# host actually prefers one is HostInfo's say (see slow_pext).
TIERS: dict[str, list[tuple[str, tuple[str, ...], frozenset[str]]]] = {
    "x86_64": [
        ("vnni512", ("vnni512", "avx512vnni", "avx512-vnni"),
         frozenset({"avx512f", "avx512bw", "avx512vl", "avx512vnni"})),
        ("avx512", ("avx512",), frozenset({"avx512f", "avx512bw", "avx512vl"})),
        # the x86-64 microarchitecture levels (what Elephant's releases are called), v4 is AVX-512, v3 AVX2 and BMI2
        ("v4", ("x86-64-v4",), frozenset({"avx512f", "avx512bw", "avx512cd", "avx512dq", "avx512vl"})),
        ("bmi2", ("bmi2", "pext"), frozenset({"avx2", "bmi2"})),
        ("v3", ("x86-64-v3",), frozenset({"avx2", "bmi1", "bmi2", "fma"})),
        ("avx2", ("avx2",), frozenset({"avx2"})),
        ("sse41", ("sse41", "sse4.1", "sse4-1", "popcnt", "modern"), frozenset({"sse4_1", "popcnt"})),
        ("v2", ("x86-64-v2",), frozenset({"ssse3", "sse4_1", "sse4_2", "popcnt"})),
        ("ssse3", ("ssse3",), frozenset({"ssse3"})),
    ],
    "arm64": [
        ("dotprod", ("dotprod",), frozenset({"dotprod"})),
        ("neon", ("neon",), frozenset({"neon"})),
    ],
}


@dataclass
class HostInfo:
    os: str                       # windows | linux | macos
    arch: str                     # x86_64 | arm64 | whatever the platform says
    cpu_name: str | None = None
    flags: frozenset[str] = frozenset()
    slow_pext: bool = False       # AMD before Zen 3 has BMI2 but PEXT is microcoded, avx2 builds are faster there
    hostname: str = field(default_factory=socket.gethostname)
    logical_cores: int = field(default_factory=lambda: os.cpu_count() or 1)

    def tiers(self) -> list[tuple[str, tuple[str, ...]]]:
        """(tier, asset names) this host can run, best first."""
        supported = [(name, aliases) for name, aliases, needs in TIERS.get(self.arch, []) if needs <= self.flags]
        override = os.environ.get("UWANJA_CPU", "").strip().lower()
        if override:
            names = [name for name, _, _ in TIERS.get(self.arch, [])]
            if override not in names:
                raise ValueError(f"UWANJA_CPU={override}: not a tier of {self.arch}, one of {', '.join(names)}")
            below = names[names.index(override):]
            return [(name, aliases) for name, aliases, _ in TIERS[self.arch] if name in below]
        if self.slow_pext:
            supported.sort(key=lambda tier: tier[0] == "bmi2")   # stable, bmi2 moves behind the rest
        return supported

    def describe(self) -> dict:
        return {"name": self.hostname, "cpu": self.cpu_name, "logical_cores": self.logical_cores}


def os_name() -> str:
    return {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}.get(platform.system(), platform.system().lower())


def arch_name() -> str:
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64", "x86-64", "x64"):
        return "x86_64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return machine


def detect() -> HostInfo:
    system, arch = os_name(), arch_name()
    flags: set[str] = set()
    name = vendor = family = None
    if arch == "x86_64":
        try:
            flags, name, vendor, family = _cpuinfo()       # the CPU's own answer, on every OS
        except ImportError:
            pass
        except Exception as failure:
            print(f"uwanja: py-cpuinfo failed ({failure}), reading the OS instead", file=sys.stderr)
    reader = {"linux": _linux, "macos": _macos, "windows": _windows}.get(system)
    if not flags and reader is not None:
        try:
            flags, name, vendor, family = reader(arch)
        except Exception as failure:     # a host we can't read still runs, with generic builds
            print(f"uwanja: couldn't read the CPU ({failure}), assuming a generic build", file=sys.stderr)
    slow = bool(vendor) and any(maker in vendor.upper() for maker in ("AMD", "HYGON")) and family in (23, 24)
    return HostInfo(system, arch, name, frozenset(flags), slow)


def parse_cpuinfo(info: dict) -> tuple[set[str], str | None, str | None, int | None]:
    """(flags, name, vendor, family) from py-cpuinfo's dictionary."""
    flags = {_flag(str(token)) for token in info.get("flags", [])}
    family = info.get("family")
    return flags, info.get("brand_raw"), info.get("vendor_id_raw"), family if isinstance(family, int) else None


def _cpuinfo():
    """CPUID through py-cpuinfo: every flag, BMI2 and the AVX-512 subsets included, which Windows' own API can't say.
    It takes a second or so, once per run."""
    import cpuinfo
    return parse_cpuinfo(cpuinfo.get_cpu_info())


def _linux(arch: str):
    flags: set[str] = set()
    name = vendor = family = None
    with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            if key in ("flags", "features") and not flags:
                flags = {_flag(token) for token in value.split()}
                if arch == "arm64":
                    flags = {"neon"} if "asimd" in flags else set()
                    if "asimddp" in value.split():
                        flags.add("dotprod")
            elif key in ("model name", "hardware") and name is None:
                name = value
            elif key == "vendor_id" and vendor is None:
                vendor = value
            elif key == "cpu family" and family is None and value.isdigit():
                family = int(value)
    return flags, name, vendor, family


def _macos(arch: str):
    def sysctl(key: str) -> str:
        result = subprocess.run(["sysctl", "-n", key], capture_output=True, text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else ""

    name = sysctl("machdep.cpu.brand_string") or None
    if arch == "arm64":
        return {"neon", "dotprod"}, name, "Apple", None     # every Apple silicon Mac has both
    tokens = (sysctl("machdep.cpu.features") + " " + sysctl("machdep.cpu.leaf7_features")).lower().split()
    flags = {_flag(token) for token in tokens}
    return flags, name, sysctl("machdep.cpu.vendor") or None, None


# IsProcessorFeaturePresent ids, the fallback when py-cpuinfo isn't there. It doesn't know BMI2 or the AVX-512
# subsets: AVX2 CPUs are taken to have BMI2 (every Intel and AMD part with AVX2 does), AVX512F to mean BW and VL
# too, and VNNI is never claimed. UWANJA_CPU can say more.
_PF = {"ssse3": 36, "sse4_1": 37, "sse4_2": 38, "avx": 39, "avx2": 40, "avx512f": 41, "arm_v8": 29, "arm_dp": 43}


def _windows(arch: str):
    import ctypes

    present = lambda key: bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(_PF[key]))   # noqa: E731
    flags: set[str] = set()
    if arch == "arm64":
        if present("arm_v8"):
            flags.add("neon")
        if present("arm_dp"):
            flags.add("dotprod")
    else:
        if present("ssse3"):
            flags.add("ssse3")
        if present("sse4_2"):
            flags |= {"sse4_1", "sse4_2", "popcnt"}      # every SSE4.2 CPU has POPCNT
        elif present("sse4_1"):
            flags.add("sse4_1")
        if present("avx2"):
            flags |= {"avx2", "bmi1", "bmi2", "fma"}
        if present("avx512f"):
            flags |= {"avx512f", "avx512bw", "avx512cd", "avx512dq", "avx512vl"}
    identifier = os.environ.get("PROCESSOR_IDENTIFIER", "")      # "AMD64 Family 25 Model 33 Stepping 2, AuthenticAMD"
    family = re.search(r"Family (\d+)", identifier)
    name = None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            name = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
    except OSError:
        pass
    vendor = identifier.rsplit(",", 1)[-1].strip() or None
    return flags, name or identifier or None, vendor, int(family[1]) if family else None


def _flag(token: str) -> str:
    """One spelling for cpuinfo's and sysctl's: lowercase, no dots, `avx512_vnni` is `avx512vnni`."""
    token = token.lower().replace(".", "_")
    return {"avx512_vnni": "avx512vnni", "avx1_0": "avx", "sse4_1": "sse4_1", "sse4_2": "sse4_2"}.get(token, token)
