"""
Guaranteed-valid fallback plan builder.

Used whenever the LP optimizer fails (infeasible, solver error, or a runtime
exception). This builder produces an all-idle plan where the battery is never
touched and solar is used up to min(solar, demand) each hour, with the
remaining demand supplied by the grid.

This plan always satisfies:
- energy balance every hour
- solar_used <= effective_solar
- battery energy constant = initial_energy_kwh (so end-of-day neutrality holds)
- base minimum reserve (since initial >= base min by request validation)
- idle => battery_kwh = 0
- all rates/bounds trivially satisfied
- no_charge / no_discharge windows trivially satisfied (idle)
- max_grid_window may be VIOLATED; caller should treat fallback as last resort.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _effective_solar_for_hour(
    hour: int,
    base_solar: float,
    solar_reductions: List[Dict[str, Any]],
) -> float:
    """Apply any solar_reduction directives whose hours list contains `hour`."""
    factor = 1.0
    for directive in solar_reductions:
        adj = directive.get("structured_adjustment") or {}
        hours = adj.get("hours") or []
        if hour in hours:
            try:
                factor *= float(adj.get("factor", 1.0))
            except (TypeError, ValueError):
                continue
    # Multiple overlapping reductions multiply; clamp into [0, 1].
    if factor < 0.0:
        factor = 0.0
    if factor > 1.0:
        factor = 1.0
    return base_solar * factor


def build_safe_plan(
    hours: List[Dict[str, Any]],
    battery: Dict[str, Any],
    directives: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Build a guaranteed-valid all-idle plan.

    Returns a dict with keys:
      hourly_plan, total_grid_kwh, total_cost_bdt, peak_grid_kwh
    """
    directives = directives or []
    solar_reductions = [
        d for d in directives if d.get("directive_type") == "solar_reduction"
    ]

    capacity = float(battery["capacity_kwh"])
    initial = float(battery["initial_energy_kwh"])

    hourly_plan: List[Dict[str, Any]] = []
    total_grid = 0.0
    total_cost = 0.0
    peak_grid = 0.0

    for h_entry in sorted(hours, key=lambda x: int(x["hour"])):
        h = int(h_entry["hour"])
        demand = float(h_entry["demand_kwh"])
        base_solar = float(h_entry["solar_kwh"])
        tariff = float(h_entry["tariff_bdt_per_kwh"])

        eff_solar = _effective_solar_for_hour(h, base_solar, solar_reductions)
        solar_used = min(eff_solar, demand)
        grid = demand - solar_used
        if grid < 0.0:
            grid = 0.0

        hourly_plan.append(
            {
                "hour": h,
                "grid_kwh": round(grid, 6),
                "solar_used_kwh": round(solar_used, 6),
                "battery_action": "idle",
                "battery_kwh": 0.0,
                "battery_energy_after_kwh": round(initial, 6),
            }
        )

        total_grid += grid
        total_cost += grid * tariff
        if grid > peak_grid:
            peak_grid = grid

    # Ensure capacity sanity (should already hold).
    if initial > capacity:
        for entry in hourly_plan:
            entry["battery_energy_after_kwh"] = round(capacity, 6)

    return {
        "hourly_plan": hourly_plan,
        "total_grid_kwh": round(total_grid, 6),
        "total_cost_bdt": round(total_cost, 6),
        "peak_grid_kwh": round(peak_grid, 6),
    }


__all__ = ["build_safe_plan"]