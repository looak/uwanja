"""Reports by changes: the schedule's digest, and which entries of a match document nyayo needs to be sent.

nyayo's `docs/match-schema.md` (*Reports by changes*) owns the rules. A report says where it starts (`report.seq`,
`report.base`) and, with a base, carries only the schedule entries that changed since the report nyayo last
acknowledged. The base can only be known where the requests are made, so the plan is made by the sender at the moment
it sends, never when the document is built: a document replaced in the queue by a newer one takes no changes with it.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

FIELDS = ("number", "round", "opponent", "subject_colour", "state", "result", "termination", "fault")


def entry_hash(entry: dict) -> int:
    """The SHA-256 of an entry's canonical JSON (the eight fields, a missing one null, keys sorted, no whitespace,
    ASCII), as a number to XOR."""
    canonical = json.dumps({name: entry.get(name) for name in FIELDS}, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True)
    return int.from_bytes(hashlib.sha256(canonical.encode("ascii")).digest(), "big")


def hex_digest(value: int) -> str:
    return f"{value:064x}"


def schedule_digest(entries: list[dict]) -> str:
    """The XOR of every entry's hash as 64 hex digits, 64 zeros for no entries."""
    value = 0
    for entry in entries:
        value ^= entry_hash(entry)
    return hex_digest(value)


@dataclass
class Held:
    """The schedule nyayo holds, as its last acknowledged answer told: the entries as sent, their hashes and the
    digest of them, kept so a report costs the entries that changed and not the schedule."""
    seq: int
    entries: dict[int, dict] = field(default_factory=dict)
    hashes: dict[int, int] = field(default_factory=dict)
    digest: int = 0


@dataclass
class Plan:
    """A report to send: the document, and what nyayo will hold if it takes it."""
    document: dict
    seq: int
    whole: bool
    entries: dict[int, dict]       # all of them for a whole report, the changed ones for a change
    hashes: dict[int, int]
    digest: int

    @property
    def digest_hex(self) -> str:
        return hex_digest(self.digest)

    def accepted(self, held: Held | None) -> Held:
        """What nyayo holds once it has answered with this report's seq and digest."""
        if self.whole or held is None:
            return Held(self.seq, dict(self.entries), dict(self.hashes), self.digest)
        held.seq, held.digest = self.seq, self.digest
        held.entries.update(self.entries)
        held.hashes.update(self.hashes)
        return held


def plan_report(document: dict, held: Held | None, seq: int) -> Plan | None:
    """The report for `document` (whole, `games` the whole schedule) at `seq`. Whole when `held` is None, or when the
    schedule lost an entry, which only a whole document can say. None for a document with no schedule: it is sent
    as it is, with no `report`."""
    games = document.get("games")
    if games is None:
        return None
    current = {entry["number"]: entry for entry in games}
    if held is not None and (len(current) != len(games) or any(number not in current for number in held.entries)):
        held = None
    if held is None:
        hashes = {number: entry_hash(entry) for number, entry in current.items()}
        digest = 0
        for value in hashes.values():
            digest ^= value
        return Plan(document | {"report": {"seq": seq, "base": None}}, seq, True, current, hashes, digest)
    changed, hashes, digest = {}, {}, held.digest
    for number, entry in current.items():
        old = held.entries.get(number)
        if old == entry:
            continue
        changed[number], hashes[number] = entry, entry_hash(entry)
        digest ^= hashes[number] ^ held.hashes.get(number, 0)
    return Plan(document | {"games": list(changed.values()), "report": {"seq": seq, "base": held.seq}},
                seq, False, changed, hashes, digest)
