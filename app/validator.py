"""
Judge-mirror self-check for POST /optimize-energy responses.

main.py calls this BEFORE sending the response. If the returned list of
violations is non-empty, the caller substitutes the fallback plan.

Checks performed (mirroring Section 11 of the Problem Statement):
  1. hourly_plan has exactly 24 unique hours 0..23
  2. all numeric values finite and non-negative
  3. energy balance every hour
  4. solar_used <= effective_solar (after solar_reduction)
  5. battery bounds: floor <= E[h] <= capacity
  6. charge/discharge rate limits
  7. idle => battery_kwh == 0
  8. no_charge / no_discharge / max_grid / reserve directives respected
  9. end-of-day neutrality E[23] == initial_energy_kwh
 10. totals (total_grid_kwh, total_cost_bdt, peak_grid_kwh) match
     values recomputed from hourly_plan
"""

from __future__ import annotations

import math
from typing import Any, Dict, List

_TOL = 1e-3  # slightly generous so 6-decimal rounding never trips the checks


def _is_finite(x: Any) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _effective_solar_per_hour(
    hours: List[Dict[str, Any]],
    directives: List[Dict[str, Any]],
) -> Dict[int, float]:
    base = {int(h["hour"]): float(h["solar_kwh"]) for h in hours}
    eff = dict(base)
    for d in directives:
        if d.get("directive_type") != "solar_reduction":
            continue
        adj = d.get("structured_adjustment") or {}
        try:
            factor = float(adj.get("factor", 1.0))
        except (TypeError, ValueError):
            continue
        factor = max(0.0, min(1.0, factor))
        for h in adj.get("hours", []) or []:
            try:
                hh = int(h)
            except (TypeError, ValueError):
                continue
            if 0 <= hh <= 23:
                eff[hh] = eff.get(hh, 0.0) * factor
    return eff


def _reserve_floor_per_hour(
    battery_min: float,
    capacity: float,
    directives: List[Dict[str, Any]],
) -> Dict[int, float]:
    floor = {h: float(battery_min) for h in range(24)}
    for d in directives:
        if d.get("directive_type") != "minimum_battery_reserve":
            continue
        adj = d.get("structured_adjustment") or {}
        try:
            reserve = float(adj.get("minimum_energy_kwh", 0.0))
        except (TypeError, ValueError):
            continue
        reserve = max(0.0, min(reserve, capacity))
        for h in adj.get("hours", []) or []:
            try:
                hh = int(h)
            except (TypeError, ValueError):
                continue
            if 0 <= hh <= 23:
                floor[hh] = max(floor[hh], reserve)
    return floor


def _no_charge_hours(directives: List[Dict[str, Any]]) -> set:
    s = set()
    for d in directives:
        if d.get("directive_type") != "no_charge_window":
            continue
        for h in (d.get("structured_adjustment") or {}).get("hours", []) or []:
            try:
                hh = int(h)
            except (TypeError, ValueError):
                continue
            if 0 <= hh <= 23:
                s.add(hh)
    return s


def _no_discharge_hours(directives: List[Dict[str, Any]]) -> set:
    s = set()
    for d in directives:
        if d.get("directive_type") != "no_discharge_window":
            continue
        for h in (d.get("structured_adjustment") or {}).get("hours", []) or []:
            try:
                hh = int(h)
            except (TypeError, ValueError):
                continue
            if 0 <= hh <= 23:
                s.add(hh)
    return s


def _max_grid_per_hour(directives: List[Dict[str, Any]]) -> Dict[int, float]:
    caps: Dict[int, float] = {}
    for d in directives:
        if d.get("directive_type") != "max_grid_window":
            continue
        adj = d.get("structured_adjustment") or {}
        try:
            cap = float(adj.get("max_grid_kwh", 0.0))
        except (TypeError, ValueError):
            continue
        cap = max(0.0, cap)
        for h in adj.get("hours", []) or []:
            try:
                hh = int(h)
            except (TypeError, ValueError):
                continue
            if 0 <= hh <= 23:
                caps[hh] = min(caps.get(hh, cap), cap)
    return caps


def validate_response(
    request_hours: List[Dict[str, Any]],
    battery: Dict[str, Any],
    directives: List[Dict[str, Any]],
    response: Dict[str, Any],
) -> List[str]:
    """
    Return a list of violation strings. Empty list means the response is valid.
    """
    violations: List[str] = []

    hourly_plan = response.get("hourly_plan")
    if not isinstance(hourly_plan, list) or len(hourly_plan) != 24:
        violations.append("hourly_plan must contain exactly 24 entries")
        return violations

    # Check hours cover 0..23 uniquely
    try:
        seen_hours = sorted(int(e["hour"]) for e in hourly_plan)
    except (KeyError, TypeError, ValueError):
        violations.append("hourly_plan entries must have an integer 'hour' field")
        return violations
    if seen_hours != list(range(24)):
        violations.append("hourly_plan must contain unique hours 0..23")

    plan_by_hour = {int(e["hour"]): e for e in hourly_plan}
    hours_by_index = {int(h["hour"]): h for h in request_hours}

    capacity = float(battery["capacity_kwh"])
    initial = float(battery["initial_energy_kwh"])
    base_min = float(battery["minimum_energy_kwh"])
    max_charge = float(battery["max_charge_kwh_per_hour"])
    max_discharge = float(battery["max_discharge_kwh_per_hour"])

    eff_solar = _effective_solar_per_hour(request_hours, directives)
    floor = _reserve_floor_per_hour(base_min, capacity, directives)
    nc_hours = _no_charge_hours(directives)
    nd_hours = _no_discharge_hours(directives)
    grid_caps = _max_grid_per_hour(directives)

    total_grid_recalc = 0.0
    total_cost_recalc = 0.0
    peak_grid_recalc = 0.0

    prev_energy = initial
    for h in range(24):
        entry = plan_by_hour[h]
        src = hours_by_index[h]

        # --- finite + non-negative numeric fields ---
        for field in (
            "grid_kwh",
            "solar_used_kwh",
            "battery_kwh",
            "battery_energy_after_kwh",
        ):
            val = entry.get(field)
            if not _is_finite(val):
                violations.append(f"hour {h}: {field} is not finite")
                continue
            if float(val) < -_TOL:
                violations.append(f"hour {h}: {field} is negative ({val})")

        grid = float(entry["grid_kwh"])
        solar = float(entry["solar_used_kwh"])
        action = entry.get("battery_action")
        batt_kwh = float(entry["battery_kwh"])
        energy_after = float(entry["battery_energy_after_kwh"])

        demand = float(src["demand_kwh"])
        base_solar = float(src["solar_kwh"])
        tariff = float(src["tariff_bdt_per_kwh"])

        # --- action consistency ---
        if action not in ("charge", "discharge", "idle"):
            violations.append(f"hour {h}: invalid battery_action {action!r}")
            continue
        if action == "idle" and abs(batt_kwh) > _TOL:
            violations.append(f"hour {h}: idle but battery_kwh = {batt_kwh}")
        if action == "charge" and batt_kwh > max_charge + _TOL:
            violations.append(
                f"hour {h}: charge {batt_kwh} exceeds max_charge {max_charge}"
            )
        if action == "discharge" and batt_kwh > max_discharge + _TOL:
            violations.append(
                f"hour {h}: discharge {batt_kwh} exceeds max_discharge {max_discharge}"
            )

        # --- energy balance ---
        # grid + solar_used + discharge == demand + charge
        discharge_mag = batt_kwh if action == "discharge" else 0.0
        charge_mag = batt_kwh if action == "charge" else 0.0
        lhs = grid + solar + discharge_mag
        rhs = demand + charge_mag
        if abs(lhs - rhs) > _TOL * max(1.0, abs(rhs)):
            violations.append(
                f"hour {h}: energy balance fails (lhs={lhs:.6f}, rhs={rhs:.6f})"
            )

        # --- solar cap ---
        cap_solar = eff_solar.get(h, 0.0)
        if solar > cap_solar + _TOL:
            violations.append(
                f"hour {h}: solar_used {solar} exceeds effective_solar {cap_solar}"
            )

        # --- battery bounds ---
        active_floor = max(base_min, float(floor.get(h, base_min)))
        if energy_after < active_floor - _TOL:
            violations.append(
                f"hour {h}: battery_energy_after {energy_after} below floor {active_floor}"
            )
        if energy_after > capacity + _TOL:
            violations.append(
                f"hour {h}: battery_energy_after {energy_after} above capacity {capacity}"
            )

        # --- E transition consistency with previous hour ---
        if action == "charge":
            expected_after = prev_energy + batt_kwh
        elif action == "discharge":
            expected_after = prev_energy - batt_kwh
        else:
            expected_after = prev_energy
        if abs(expected_after - energy_after) > _TOL * max(1.0, abs(expected_after)):
            violations.append(
                f"hour {h}: battery transition mismatch "
                f"(expected {expected_after}, got {energy_after})"
            )

        # --- directive windows ---
        if h in nc_hours and action == "charge" and batt_kwh > _TOL:
            violations.append(f"hour {h}: charge forbidden by no_charge_window")
        if h in nd_hours and action == "discharge" and batt_kwh > _TOL:
            violations.append(f"hour {h}: discharge forbidden by no_discharge_window")
        if h in grid_caps and grid > grid_caps[h] + _TOL:
            violations.append(
                f"hour {h}: grid {grid} exceeds max_grid_window cap {grid_caps[h]}"
            )

        # Totals recomputed from hourly_plan
        total_grid_recalc += grid
        total_cost_recalc += grid * tariff
        if grid > peak_grid_recalc:
            peak_grid_recalc = grid

        prev_energy = energy_after

    # --- end-of-day neutrality ---
    final_energy = float(plan_by_hour[23]["battery_energy_after_kwh"])
    if abs(final_energy - initial) > _TOL * max(1.0, abs(initial)):
        violations.append(
            f"end-of-day neutrality violated (E[23]={final_energy}, initial={initial})"
        )

    # --- totals consistency ---
    def _close(a: float, b: float) -> bool:
        return abs(a - b) <= max(0.01, _TOL * max(1.0, abs(b)))

    r_total_grid = response.get("total_grid_kwh")
    r_total_cost = response.get("total_cost_bdt")
    r_peak_grid = response.get("peak_grid_kwh")

    if not _is_finite(r_total_grid) or not _close(float(r_total_grid), total_grid_recalc):
        violations.append(
            f"total_grid_kwh mismatch: response={r_total_grid}, recalc={total_grid_recalc:.6f}"
        )
    if not _is_finite(r_total_cost) or not _close(float(r_total_cost), total_cost_recalc):
        violations.append(
            f"total_cost_bdt mismatch: response={r_total_cost}, recalc={total_cost_recalc:.6f}"
        )
    if not _is_finite(r_peak_grid) or not _close(float(r_peak_grid), peak_grid_recalc):
        violations.append(
            f"peak_grid_kwh mismatch: response={r_peak_grid}, recalc={peak_grid_recalc:.6f}"
        )

    return violations


__all__ = ["validate_response"]