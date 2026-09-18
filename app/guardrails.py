"""
Deterministic guardrails for LLM-produced directive interpretations.

The LLM is treated as untrusted. This module REPAIRS the LLM output rather
than discarding it, so the optimizer always receives something well-formed.

Repair policy (in order):
  1. Ensure exactly N entries, reindexed 0..N-1. Fill missing with no_op.
     Drop duplicates, keeping the first occurrence per note_index.
  2. Unknown / invalid directive_type  -> no_op.
  3. hours: coerce to int, drop out-of-range, dedupe, sort ascending.
     If empty after cleaning  -> no_op.
  4. factor: clamp to [0, 1]. Missing  -> no_op.
  5. minimum_energy_kwh: finite, >= 0, clamp to capacity. Missing -> no_op.
  6. max_grid_kwh: finite, >= 0. Missing -> no_op.
  7. Enforce no_op semantics: applies=False, structured_adjustment=None.
     Enforce non-no_op semantics: applies=True, structured_adjustment present.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


_ALLOWED_TYPES = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _is_finite_number(v: Any) -> bool:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f)


def _coerce_hours(raw: Any) -> List[int]:
    """Coerce, dedupe, sort, and range-clamp an hours list."""
    if not isinstance(raw, (list, tuple)):
        return []
    seen = set()
    out: List[int] = []
    for item in raw:
        if isinstance(item, bool):
            continue
        if isinstance(item, (int, float)):
            try:
                h = int(round(float(item)))
            except (TypeError, ValueError):
                continue
        elif isinstance(item, str):
            s = item.strip()
            if not s:
                continue
            try:
                h = int(round(float(s)))
            except (TypeError, ValueError):
                continue
        else:
            continue
        if 0 <= h <= 23 and h not in seen:
            seen.add(h)
            out.append(h)
    out.sort()
    return out


def _make_no_op(note_index: int, explanation: str) -> Dict[str, Any]:
    return {
        "note_index": note_index,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": explanation,
    }


# ---------------------------------------------------------------------------
# Per-entry repair
# ---------------------------------------------------------------------------


def _repair_one(
    entry: Any,
    note_index: int,
    battery_capacity_kwh: float,
) -> Dict[str, Any]:
    """Repair a single LLM directive entry. Always returns a valid dict."""

    if not isinstance(entry, dict):
        return _make_no_op(note_index, "Unparseable directive; treated as no_op.")

    raw_type = entry.get("directive_type")
    if not isinstance(raw_type, str) or raw_type not in _ALLOWED_TYPES:
        return _make_no_op(
            note_index,
            "Unknown or missing directive_type; treated as no_op.",
        )

    dt = raw_type

    if dt == "no_op":
        return _make_no_op(
            note_index,
            str(entry.get("explanation") or "Note does not affect the schedule.")[:500],
        )

    raw_adj = entry.get("structured_adjustment")
    if not isinstance(raw_adj, dict):
        return _make_no_op(
            note_index,
            f"{dt} missing structured_adjustment; treated as no_op.",
        )

    explanation = str(entry.get("explanation") or "")[:500]
    hours = _coerce_hours(raw_adj.get("hours"))
    if not hours:
        return _make_no_op(
            note_index,
            f"{dt} had no valid hours; treated as no_op.",
        )

    # Per-type numeric handling
    if dt == "solar_reduction":
        factor_raw = raw_adj.get("factor")
        if not _is_finite_number(factor_raw):
            return _make_no_op(
                note_index,
                "solar_reduction missing valid factor; treated as no_op.",
            )
        factor = max(0.0, min(1.0, float(factor_raw)))
        return {
            "note_index": note_index,
            "applies": True,
            "directive_type": "solar_reduction",
            "structured_adjustment": {"hours": hours, "factor": factor},
            "explanation": explanation or "Usable solar reduced during the listed hours.",
        }

    if dt == "minimum_battery_reserve":
        min_raw = raw_adj.get("minimum_energy_kwh")
        if not _is_finite_number(min_raw):
            return _make_no_op(
                note_index,
                "minimum_battery_reserve missing valid minimum_energy_kwh; treated as no_op.",
            )
        min_kwh = max(0.0, float(min_raw))
        if battery_capacity_kwh > 0:
            min_kwh = min(min_kwh, float(battery_capacity_kwh))
        return {
            "note_index": note_index,
            "applies": True,
            "directive_type": "minimum_battery_reserve",
            "structured_adjustment": {
                "hours": hours,
                "minimum_energy_kwh": min_kwh,
            },
            "explanation": explanation
            or "Battery must stay at or above the stated reserve during the listed hours.",
        }

    if dt in ("no_charge_window", "no_discharge_window"):
        return {
            "note_index": note_index,
            "applies": True,
            "directive_type": dt,
            "structured_adjustment": {"hours": hours},
            "explanation": explanation
            or ("Battery charging disabled during the listed hours."
                if dt == "no_charge_window"
                else "Battery discharge disabled during the listed hours."),
        }

    if dt == "max_grid_window":
        cap_raw = raw_adj.get("max_grid_kwh")
        if not _is_finite_number(cap_raw):
            return _make_no_op(
                note_index,
                "max_grid_window missing valid max_grid_kwh; treated as no_op.",
            )
        cap = max(0.0, float(cap_raw))
        return {
            "note_index": note_index,
            "applies": True,
            "directive_type": "max_grid_window",
            "structured_adjustment": {"hours": hours, "max_grid_kwh": cap},
            "explanation": explanation
            or "Grid import capped during the listed hours.",
        }

    # Should never reach here because of the _ALLOWED_TYPES check above
    return _make_no_op(note_index, "Unhandled directive type; treated as no_op.")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def guardrail_directives(
    raw_directives: Any,
    num_notes: int,
    battery_capacity_kwh: float,
) -> List[Dict[str, Any]]:
    """
    Validate and repair raw LLM directives so the optimizer can trust them.

    Always returns a list of length `num_notes` with note_index = 0..num_notes-1,
    each entry having the correct shape and semantics.
    """
    if num_notes <= 0:
        return []

    if not isinstance(raw_directives, list):
        raw_directives = []

    # 1. Bucket entries by note_index, keeping the first occurrence only.
    by_index: Dict[int, Any] = {}
    for entry in raw_directives:
        if not isinstance(entry, dict):
            continue
        idx_raw = entry.get("note_index")
        if isinstance(idx_raw, bool):
            continue
        try:
            idx = int(idx_raw)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < num_notes and idx not in by_index:
            by_index[idx] = entry

    # 2. Repair each slot; fill missing with no_op.
    repaired: List[Dict[str, Any]] = []
    for i in range(num_notes):
        if i in by_index:
            repaired.append(_repair_one(by_index[i], i, battery_capacity_kwh))
        else:
            repaired.append(
                _make_no_op(
                    i,
                    "LLM did not return an entry for this note; treated as no_op.",
                )
            )

    logger.info("Guardrails produced %d directive(s) for %d note(s)", len(repaired), num_notes)
    return repaired


__all__ = ["guardrail_directives"]