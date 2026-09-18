"""
Linear-programming optimizer for the GridWise challenge.

Decision variables per hour h = 0..23:
  g[h]  grid_kwh            >= 0
  s[h]  solar_used_kwh      >= 0, <= effective_solar[h]
  c[h]  battery charge      >= 0, <= max_charge_kwh_per_hour
  d[h]  battery discharge   >= 0, <= max_discharge_kwh_per_hour
  E[h]  battery energy after hour h

Constraints:
  - balance:  g[h] + s[h] + d[h] == demand[h] + c[h]
  - E[0]  = initial + c[0] - d[0]
  - E[h]  = E[h-1] + c[h] - d[h]   for h >= 1
  - base minimum <= E[h] <= capacity
  - minimum_battery_reserve directives raise the per-hour floor
  - no_charge_window:      c[h] == 0
  - no_discharge_window:   d[h] == 0
  - max_grid_window:       g[h] <= max_grid_kwh
  - END-OF-DAY NEUTRALITY: E[23] == initial_energy_kwh

Objective: minimize SUM(g[h] * tariff[h]).

After solving, we do THREE post-processing steps (per the spec):
  (a) net out hours where both c[h] and d[h] > 0
  (b) recompute grid_kwh = demand + charge - discharge - solar_used,
      reducing solar_used if grid would go negative; clamp tiny negatives.
  (c) recompute E[h] cumulatively from the FINAL charge/discharge values
      (do NOT read E from the LP).

If the LP is infeasible or the solver throws, return None so the caller can
degrade to the all-idle fallback plan.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import pulp

logger = logging.getLogger(__name__)


_ROUND = 6


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _effective_solar_per_hour(
    hours: List[Dict[str, Any]],
    directives: List[Dict[str, Any]],
) -> Dict[int, float]:
    """Compute effective solar (base * product of applicable factors)."""
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
    """Base minimum, raised by any minimum_battery_reserve directives."""
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
                # If multiple caps overlap, use the tightest.
                if hh in caps:
                    caps[hh] = min(caps[hh], cap)
                else:
                    caps[hh] = cap
    return caps


# ---------------------------------------------------------------------------
# Post-processing
# ---------------------------------------------------------------------------


def _post_process(
    hours_sorted: List[Dict[str, Any]],
    initial_energy: float,
    capacity: float,
    base_min: float,
    floor: Dict[int, float],
    charge: Dict[int, float],
    discharge: Dict[int, float],
    solar_used: Dict[int, float],
    effective_solar: Dict[int, float],
) -> Tuple[List[Dict[str, Any]], float, float, float]:
    """
    Apply the three post-processing steps and produce the final hourly_plan.
    Returns (hourly_plan, total_grid, total_cost, peak_grid).
    """
    # (a) Net out simultaneous charge & discharge.
    for h in range(24):
        c = charge.get(h, 0.0)
        d = discharge.get(h, 0.0)
        if c > 0 and d > 0:
            net = c - d
            if net >= 0:
                charge[h] = net
                discharge[h] = 0.0
            else:
                charge[h] = 0.0
                discharge[h] = -net

    # (b) Recompute grid from balance; adjust solar_used if grid would go negative.
    grid: Dict[int, float] = {}
    for h in range(24):
        entry = hours_sorted[h]
        demand = float(entry["demand_kwh"])
        c = charge.get(h, 0.0)
        d = discharge.get(h, 0.0)
        su = solar_used.get(h, 0.0)

        g = demand + c - d - su
        if g < 0.0:
            # Reduce solar_used to make g exactly 0; never increase it.
            su = demand + c - d
            if su < 0.0:
                su = 0.0
            cap_solar = effective_solar.get(h, 0.0)
            if su > cap_solar:
                su = cap_solar
            g = demand + c - d - su
            if g < -1e-9:
                g = 0.0
        if g < 0.0:
            g = 0.0

        grid[h] = g
        solar_used[h] = su

    # (c) Recompute E cumulatively from final c/d (do NOT read E from LP).
    hourly_plan: List[Dict[str, Any]] = []
    total_grid = 0.0
    total_cost = 0.0
    peak_grid = 0.0

    energy = float(initial_energy)
    for h in range(24):
        entry = hours_sorted[h]
        c = charge.get(h, 0.0)
        d = discharge.get(h, 0.0)

        if c > 1e-9 and d > 1e-9:
            action = "idle"
            magnitude = 0.0
            energy_after = energy
        elif c > 1e-9:
            action = "charge"
            magnitude = c
            energy_after = energy + c
        elif d > 1e-9:
            action = "discharge"
            magnitude = d
            energy_after = energy - d
        else:
            action = "idle"
            magnitude = 0.0
            energy_after = energy

        # Clamp into [floor, capacity] defensively.
        f = max(float(base_min), float(floor.get(h, base_min)))
        if energy_after < f - 1e-9:
            energy_after = f
        if energy_after > capacity + 1e-9:
            energy_after = capacity

        g = grid[h]
        su = solar_used[h]
        tariff = float(entry["tariff_bdt_per_kwh"])

        hourly_plan.append(
            {
                "hour": h,
                "grid_kwh": round(max(0.0, g), _ROUND),
                "solar_used_kwh": round(max(0.0, su), _ROUND),
                "battery_action": action,
                "battery_kwh": round(max(0.0, magnitude), _ROUND),
                "battery_energy_after_kwh": round(max(0.0, energy_after), _ROUND),
            }
        )

        energy = energy_after
        total_grid += max(0.0, g)
        total_cost += max(0.0, g) * tariff
        if g > peak_grid:
            peak_grid = g

    return (
        hourly_plan,
        round(total_grid, _ROUND),
        round(total_cost, _ROUND),
        round(peak_grid, _ROUND),
    )


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def optimize(
    hours: List[Dict[str, Any]],
    battery: Dict[str, Any],
    directives: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Solve the LP and return a dict with:
      hourly_plan, total_grid_kwh, total_cost_bdt, peak_grid_kwh
    Returns None if the LP is infeasible or the solver raises.
    """
    try:
        hours_sorted = sorted(hours, key=lambda x: int(x["hour"]))
        if len(hours_sorted) != 24:
            raise ValueError("hours must contain exactly 24 entries")

        capacity = float(battery["capacity_kwh"])
        initial = float(battery["initial_energy_kwh"])
        base_min = float(battery["minimum_energy_kwh"])
        max_charge = float(battery["max_charge_kwh_per_hour"])
        max_discharge = float(battery["max_discharge_kwh_per_hour"])

        effective_solar = _effective_solar_per_hour(hours_sorted, directives)
        floor = _reserve_floor_per_hour(base_min, capacity, directives)
        nc_hours = _no_charge_hours(directives)
        nd_hours = _no_discharge_hours(directives)
        grid_cap = _max_grid_per_hour(directives)

        # Sanity: base floor must not exceed capacity.
        for h in range(24):
            if floor[h] > capacity + 1e-9:
                logger.warning("Reserve floor exceeds capacity at hour %d", h)
                floor[h] = capacity

        # ---- Build LP ----
        prob = pulp.LpProblem("gridwise_optimize", pulp.LpMinimize)

        g = {h: pulp.LpVariable(f"g_{h}", lowBound=0) for h in range(24)}
        s = {
            h: pulp.LpVariable(
                f"s_{h}", lowBound=0, upBound=effective_solar[h]
            )
            for h in range(24)
        }
        c = {
            h: pulp.LpVariable(f"c_{h}", lowBound=0, upBound=max_charge)
            for h in range(24)
        }
        d = {
            h: pulp.LpVariable(f"d_{h}", lowBound=0, upBound=max_discharge)
            for h in range(24)
        }
        E = {
            h: pulp.LpVariable(
                f"E_{h}", lowBound=floor[h], upBound=capacity
            )
            for h in range(24)
        }

        # Objective: minimize sum(g[h] * tariff[h])
        prob += pulp.lpSum(
            g[h] * float(hours_sorted[h]["tariff_bdt_per_kwh"]) for h in range(24)
        )

        # Energy balance per hour
        for h in range(24):
            demand = float(hours_sorted[h]["demand_kwh"])
            prob += g[h] + s[h] + d[h] == demand + c[h]

        # Battery state transitions
        prob += E[0] == initial + c[0] - d[0]
        for h in range(1, 24):
            prob += E[h] == E[h - 1] + c[h] - d[h]

        # no_charge / no_discharge windows
        for h in nc_hours:
            prob += c[h] == 0
        for h in nd_hours:
            prob += d[h] == 0

        # max_grid_window caps
        for h, cap in grid_cap.items():
            prob += g[h] <= cap

        # End-of-day neutrality
        prob += E[23] == initial

        # Solve
        solver = pulp.PULP_CBC_CMD(msg=False)
        status = prob.solve(solver)

        if pulp.LpStatus[status] != "Optimal":
            logger.warning(
                "LP status=%s; falling back to safe plan",
                pulp.LpStatus.get(status, "Unknown"),
            )
            return None

        charge_v = {h: max(0.0, float(pulp.value(c[h]) or 0.0)) for h in range(24)}
        discharge_v = {h: max(0.0, float(pulp.value(d[h]) or 0.0)) for h in range(24)}
        solar_v = {h: max(0.0, float(pulp.value(s[h]) or 0.0)) for h in range(24)}

        hourly_plan, total_grid, total_cost, peak_grid = _post_process(
            hours_sorted,
            initial,
            capacity,
            base_min,
            floor,
            charge_v,
            discharge_v,
            solar_v,
            effective_solar,
        )

        return {
            "hourly_plan": hourly_plan,
            "total_grid_kwh": total_grid,
            "total_cost_bdt": total_cost,
            "peak_grid_kwh": peak_grid,
        }

    except Exception as exc:  # noqa: BLE001 - spec requires graceful degradation
        logger.error("Optimizer failed with %s; falling back to safe plan", type(exc).__name__)
        return None


__all__ = ["optimize"]