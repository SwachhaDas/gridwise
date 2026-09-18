"""
Pydantic v2 models for the GridWise LLM-assisted energy optimization service.

Covers:
- Request schema: scenario_id, operator_notes, hours[24], battery
- Response schema: directive_interpretation, hourly_plan[24], totals, plan_summary
- Directive sub-schemas for all 6 supported directive types
"""

from __future__ import annotations

from typing import Annotated, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Directive type literals
# ---------------------------------------------------------------------------

DirectiveType = Literal[
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
]

BatteryAction = Literal["charge", "discharge", "idle"]


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class HourEntry(BaseModel):
    """One hourly interval of the 24-hour planning horizon."""

    model_config = ConfigDict(extra="forbid")

    hour: int = Field(..., ge=0, le=23, description="Hour index 0..23")
    demand_kwh: float = Field(..., ge=0, description="Campus demand in kWh")
    solar_kwh: float = Field(..., ge=0, description="Base solar availability in kWh")
    tariff_bdt_per_kwh: float = Field(
        ..., ge=0, description="Grid tariff for this hour in BDT/kWh"
    )

    @field_validator("hour")
    @classmethod
    def _hour_must_be_int(cls, v: int) -> int:
        if not isinstance(v, int):
            raise ValueError("hour must be an integer")
        return v


class BatteryConfig(BaseModel):
    """Battery energy storage configuration for the scenario."""

    model_config = ConfigDict(extra="forbid")

    capacity_kwh: float = Field(..., gt=0, description="Battery capacity (>0)")
    initial_energy_kwh: float = Field(..., ge=0, description="Energy at start of hour 0")
    minimum_energy_kwh: float = Field(..., ge=0, description="Base minimum reserve")
    max_charge_kwh_per_hour: float = Field(
        ..., ge=0, description="Max charge rate per hour"
    )
    max_discharge_kwh_per_hour: float = Field(
        ..., ge=0, description="Max discharge rate per hour"
    )

    @model_validator(mode="after")
    def _sanity_checks(self) -> "BatteryConfig":
        if self.initial_energy_kwh > self.capacity_kwh:
            raise ValueError("initial_energy_kwh cannot exceed capacity_kwh")
        if self.minimum_energy_kwh > self.capacity_kwh:
            raise ValueError("minimum_energy_kwh cannot exceed capacity_kwh")
        if self.initial_energy_kwh < self.minimum_energy_kwh:
            raise ValueError(
                "initial_energy_kwh must be >= minimum_energy_kwh for a feasible scenario"
            )
        return self


class OptimizeRequest(BaseModel):
    """POST /optimize-energy request body."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(..., min_length=1, max_length=200)
    operator_notes: Annotated[List[str], Field(min_length=1, max_length=3)]
    hours: Annotated[List[HourEntry], Field(min_length=24, max_length=24)]
    battery: BatteryConfig

    @field_validator("operator_notes")
    @classmethod
    def _notes_non_empty(cls, v: List[str]) -> List[str]:
        cleaned: List[str] = []
        for note in v:
            if not isinstance(note, str):
                raise ValueError("every operator note must be a string")
            stripped = note.strip()
            if not stripped:
                raise ValueError("operator notes must be non-empty strings")
            cleaned.append(stripped)
        if not (1 <= len(cleaned) <= 3):
            raise ValueError("operator_notes must contain 1 to 3 items")
        return cleaned

    @field_validator("hours")
    @classmethod
    def _hours_must_cover_0_to_23(cls, v: List[HourEntry]) -> List[HourEntry]:
        seen = sorted(h.hour for h in v)
        expected = list(range(24))
        if seen != expected:
            raise ValueError(
                "hours must contain exactly 24 unique entries with hour values 0..23"
            )
        # sort ascending to keep deterministic order
        return sorted(v, key=lambda h: h.hour)


# ---------------------------------------------------------------------------
# Directive sub-schemas
# ---------------------------------------------------------------------------


def _unique_sorted_hours(hours: List[int]) -> List[int]:
    """Coerce, dedupe, sort, and range-clamp an hours list."""
    out: List[int] = []
    seen = set()
    for raw in hours:
        try:
            h = int(round(float(raw)))
        except (TypeError, ValueError):
            continue
        if 0 <= h <= 23 and h not in seen:
            seen.add(h)
            out.append(h)
    out.sort()
    return out


class SolarReductionAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hours: List[int]
    factor: float = Field(..., ge=0, le=1)

    @field_validator("hours")
    @classmethod
    def _clean_hours(cls, v: List[int]) -> List[int]:
        return _unique_sorted_hours(v)


class MinimumBatteryReserveAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hours: List[int]
    minimum_energy_kwh: float = Field(..., ge=0)

    @field_validator("hours")
    @classmethod
    def _clean_hours(cls, v: List[int]) -> List[int]:
        return _unique_sorted_hours(v)


class WindowHoursAdjustment(BaseModel):
    """Used for no_charge_window and no_discharge_window."""

    model_config = ConfigDict(extra="forbid")
    hours: List[int]

    @field_validator("hours")
    @classmethod
    def _clean_hours(cls, v: List[int]) -> List[int]:
        return _unique_sorted_hours(v)


class MaxGridWindowAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hours: List[int]
    max_grid_kwh: float = Field(..., ge=0)

    @field_validator("hours")
    @classmethod
    def _clean_hours(cls, v: List[int]) -> List[int]:
        return _unique_sorted_hours(v)


# ---------------------------------------------------------------------------
# Directive interpretation entry (what the LLM returns, post-guardrail)
# ---------------------------------------------------------------------------


class DirectiveInterpretation(BaseModel):
    """
    One interpretation entry per operator note, in note_index order.

    Validation rules (mirrors the Problem Statement):
      - no_op is the ONLY directive allowed with applies=False
      - no_op MUST have structured_adjustment=None
      - every other directive MUST have applies=True and a structured_adjustment
    """

    model_config = ConfigDict(extra="forbid")

    note_index: int = Field(..., ge=0, le=2)
    applies: bool
    directive_type: DirectiveType
    structured_adjustment: Optional[
        Union[
            SolarReductionAdjustment,
            MinimumBatteryReserveAdjustment,
            WindowHoursAdjustment,
            MaxGridWindowAdjustment,
            dict,
            None,
        ]
    ] = None
    explanation: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def _enforce_directive_semantics(self) -> "DirectiveInterpretation":
        dt = self.directive_type

        if dt == "no_op":
            # Force no_op semantics regardless of what the LLM sent.
            object.__setattr__(self, "applies", False)
            object.__setattr__(self, "structured_adjustment", None)
            return self

        # Non-no_op directives MUST be applicable and MUST carry an adjustment.
        object.__setattr__(self, "applies", True)
        if self.structured_adjustment is None:
            raise ValueError(
                f"directive_type={dt!r} requires a non-null structured_adjustment"
            )

        # Coerce dict payloads into the correct sub-model per directive type.
        adj = self.structured_adjustment
        if isinstance(adj, dict):
            if dt == "solar_reduction":
                adj = SolarReductionAdjustment(**adj)
            elif dt == "minimum_battery_reserve":
                adj = MinimumBatteryReserveAdjustment(**adj)
            elif dt in ("no_charge_window", "no_discharge_window"):
                adj = WindowHoursAdjustment(**adj)
            elif dt == "max_grid_window":
                adj = MaxGridWindowAdjustment(**adj)
            object.__setattr__(self, "structured_adjustment", adj)

        return self


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class HourlyPlanEntry(BaseModel):
    """One hour of the final 24-hour schedule."""

    model_config = ConfigDict(extra="forbid")

    hour: int = Field(..., ge=0, le=23)
    grid_kwh: float = Field(..., ge=0)
    solar_used_kwh: float = Field(..., ge=0)
    battery_action: BatteryAction
    battery_kwh: float = Field(..., ge=0)
    battery_energy_after_kwh: float = Field(..., ge=0)

    @model_validator(mode="after")
    def _idle_zero(self) -> "HourlyPlanEntry":
        if self.battery_action == "idle" and self.battery_kwh != 0:
            raise ValueError("battery_kwh must be 0 when battery_action == 'idle'")
        return self


class OptimizeResponse(BaseModel):
    """POST /optimize-energy success response."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    directive_interpretation: List[DirectiveInterpretation]
    hourly_plan: Annotated[List[HourlyPlanEntry], Field(min_length=24, max_length=24)]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str = ""

    @field_validator("hourly_plan")
    @classmethod
    def _hours_cover_0_to_23(cls, v: List[HourlyPlanEntry]) -> List[HourlyPlanEntry]:
        seen = sorted(h.hour for h in v)
        if seen != list(range(24)):
            raise ValueError("hourly_plan must contain exactly hours 0..23")
        return sorted(v, key=lambda h: h.hour)


# ---------------------------------------------------------------------------
# Health response
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = "ok"


__all__ = [
    "DirectiveType",
    "BatteryAction",
    "HourEntry",
    "BatteryConfig",
    "OptimizeRequest",
    "SolarReductionAdjustment",
    "MinimumBatteryReserveAdjustment",
    "WindowHoursAdjustment",
    "MaxGridWindowAdjustment",
    "DirectiveInterpretation",
    "HourlyPlanEntry",
    "OptimizeResponse",
    "HealthResponse",
]