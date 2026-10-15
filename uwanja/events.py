"""Engine I/O events, one per UCI line between a match runner and an engine.

Every source (fastchess log, cutechess debug output, a UCI proxy) turns its input into EngineIO events, the tracker
only ever sees those.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

TO_ENGINE = "in"
FROM_ENGINE = "out"


@dataclass(frozen=True)
class EngineIO:
    time: float      # seconds, only differences matter
    slot: str        # the runner's game slot, games in one slot run one after another
    engine: str      # the engine's name in the match
    direction: str   # TO_ENGINE or FROM_ENGINE
    text: str


# [Engine] [21:11:14.495571] <     131159490374448>  base <--- go wtime 1010 btime 1010 winc 10 binc 10
_FASTCHESS = re.compile(
    r"^\[Engine\]\s*\[(\d+):(\d+):(\d+(?:\.\d+)?)\]\s*<\s*(\d+)>\s+(.+?) (<---|--->) ?(.*)$")


def parse_fastchess_line(line: str) -> EngineIO | None:
    """A line of a fastchess log written with -log level=info engine=true, None for anything else."""
    match = _FASTCHESS.match(line.rstrip("\r\n"))
    if match is None:
        return None
    hours, minutes, seconds, slot, engine, arrow, text = match.groups()
    time = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return EngineIO(time, slot, engine.strip(), TO_ENGINE if arrow == "<---" else FROM_ENGINE, text.strip())
