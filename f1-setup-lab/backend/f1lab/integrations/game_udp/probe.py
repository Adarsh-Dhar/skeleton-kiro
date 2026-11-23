"""Setup-probe analysis: which decoded Car Setups field moved, and did it match the catalog's expected field?

The probe only produces EVIDENCE. It never marks a factor verified: that needs a person who compared the packet values
with the numbers shown in the game garage (``scripts/verify_setup_mapping.py``).

Guided use: ``mark(factor)`` declares which garage control the user is about to move; every setup change until the
next ``mark`` is attributed to that control, so the report can say "you moved X and field Y changed".
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

GAME_VARIANTS = {2025: "f1-25", 2026: "f1-25-2026"}
VARIANT_LABELS = {"f1-25": "EA SPORTS F1 25", "f1-25-2026": "F1 25: 2026 Season Pack"}


def variant_of(packet_format: int | None) -> str | None:
    return GAME_VARIANTS.get(packet_format) if packet_format is not None else None


class SetupProbe:
    def __init__(self, expected: dict[str, str]) -> None:
        """``expected`` maps catalog factor -> packet field (from the catalog)."""
        self.expected = dict(expected)
        self.initial: dict[str, float] | None = None
        self.last: dict[str, float] | None = None
        self.changes: list[dict[str, Any]] = []
        self.formats: set[int] = set()
        self.setup_packets = 0
        self.current_control: str | None = None
        self.marks: list[dict[str, Any]] = []
        self.started_at = datetime.now(UTC)

    def mark(self, factor: str | None, t: float | None = None) -> None:
        if factor is not None and factor not in self.expected:
            raise ValueError(f"unknown factor '{factor}'; expected one of {sorted(self.expected)}")
        self.current_control = factor
        self.marks.append({"factor": factor, "t": t, "after_change": len(self.changes)})

    def observe(self, setup: dict[str, float], t: float | None = None, packet_format: int | None = None) -> dict | None:
        """Feed one decoded setup. Returns the change event (or None when nothing changed)."""
        self.setup_packets += 1
        if packet_format is not None:
            self.formats.add(int(packet_format))
        setup = {k: (float(v) if isinstance(v, (int, float)) else v) for k, v in setup.items()}
        if self.last is None:
            self.initial = dict(setup)
            self.last = dict(setup)
            return None
        changed = {k: [self.last.get(k), v] for k, v in setup.items() if self.last.get(k) != v}
        self.last = dict(setup)
        if not changed:
            return None
        ev = {"index": len(self.changes), "t": t, "packet_format": packet_format, "control_moved": self.current_control, "changed": changed}
        self.changes.append(ev)
        return ev

    # ------------------------------------------------------------------ report
    def _values_seen(self, field: str) -> list[float]:
        vals = set()
        if self.initial and field in self.initial:
            vals.add(self.initial[field])
        for ev in self.changes:
            if field in ev["changed"]:
                vals.update(v for v in ev["changed"][field] if v is not None)
        return sorted(vals)

    def _finding(self, factor: str) -> dict[str, Any]:
        field = self.expected[factor]
        touching = [ev for ev in self.changes if field in ev["changed"]]
        isolated = [ev for ev in touching if set(ev["changed"]) == {field}]
        co_moved = sorted({k for ev in touching for k in ev["changed"] if k != field})
        guided = [ev for ev in self.changes if ev["control_moved"] == factor]
        guided_fields = sorted({k for ev in guided for k in ev["changed"]})
        vals = self._values_seen(field)
        if guided:
            if guided_fields == [field]:
                status = "matches_expected_field"
            elif field in guided_fields:
                status = "expected_field_moved_with_others"
            else:
                status = "different_field_changed"
        elif not touching:
            status = "no_change_observed"
        elif isolated:
            status = "isolated_change_observed"
        else:
            status = "moved_with_other_fields"
        notes = []
        if len(vals) < 2:
            notes.append("fewer than two distinct values seen: move the control to its lowest AND highest position")
        if co_moved:
            notes.append(f"other fields changed in the same packets: {', '.join(co_moved)}")
        if guided and status == "different_field_changed":
            notes.append(f"while moving {factor} the packet changed {', '.join(guided_fields)} instead of {field}: the catalog mapping may be wrong")
        return {
            "factor": factor, "expected_packet_field": field, "status": status, "verified": False,
            "values_seen": vals, "low_seen": vals[0] if vals else None, "high_seen": vals[-1] if vals else None,
            "changes_touching_field": len(touching), "isolated_changes": len(isolated), "co_moved_fields": co_moved,
            "guided_changes": len(guided), "fields_changed_while_guided": guided_fields, "notes": notes,
        }  # fmt: skip

    def report(self, report_id: str | None = None) -> dict[str, Any]:
        expected_fields = set(self.expected.values())
        unexpected = sorted({k for ev in self.changes for k in ev["changed"] if k not in expected_fields})
        variants = sorted({v for v in (variant_of(f) for f in self.formats) if v})
        return {
            "report_id": report_id or "probe-" + uuid.uuid4().hex[:10],
            "created_at": datetime.now(UTC).isoformat(), "started_at": self.started_at.isoformat(),
            "packet_formats": sorted(self.formats), "game_variants": variants,
            "game_variant_labels": [VARIANT_LABELS[v] for v in variants],
            "setup_packets": self.setup_packets, "initial_setup": self.initial, "changes": self.changes, "marks": self.marks,
            "factors": {f: self._finding(f) for f in self.expected},
            "other_fields_changed": unexpected,
            "verified": False,
            "verification_note": "Evidence only. Nothing is verified automatically: compare these values with the numbers in the game garage, "
                                 "then record your decision with scripts/verify_setup_mapping.py.",
        }  # fmt: skip
