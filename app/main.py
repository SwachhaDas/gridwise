"""
FastAPI application for the GridWise LLM-assisted energy optimization service.

Endpoints:
  GET  /health            -> {"status": "ok"}
  POST /optimize-energy   -> interpretation + 24-hour schedule

Orchestration:
  1. Validate request (Pydantic -> 400/422).
  2. Interpret operator notes via DeepSeek LLM.
  3. Guardrail-repair the LLM output.
  4. Run the LP optimizer with the directives applied.
  5. Self-validate the response (judge mirror).
  6. If step 4 or 5 fails, substitute the guaranteed-valid fallback plan.

Error handling:
  400 malformed JSON or structurally invalid request
  422 well-formed but semantically invalid request
  500 controlled internal error with generic message only
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.fallback import build_safe_plan
from app.guardrails import guardrail_directives
from app.llm import interpret_notes
from app.models import HealthResponse, OptimizeRequest, OptimizeResponse
from app.optimizer import optimize
from app.validator import validate_response

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

load_dotenv()  # read .env if present

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("gridwise.main")


app = FastAPI(
    title="GridWise LLM-Assisted Energy Optimization",
    version="1.0.0",
    description="BUP CSE Fest 2026 · Preliminary · GridWise challenge",
)


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """
    FastAPI raises this for malformed JSON or schema violations.

    We map:
      - JSON parse errors ("json_invalid") -> 400
      - everything else (schema issues)    -> 422
    """
    errors = exc.errors() or []
    is_malformed_json = any(
        (e.get("type") in ("json_invalid", "value_error.jsondecode"))
        or ("JSON" in str(e.get("msg", "")))
        for e in errors
    )
    status = 400 if is_malformed_json else 422
    return JSONResponse(
        status_code=status,
        content={
            "detail": "Malformed JSON body" if status == 400 else "Invalid request body",
            "errors": [
                {"loc": list(e.get("loc", [])), "msg": e.get("msg", "")} for e in errors
            ],
        },
    )


@app.exception_handler(ValidationError)
async def _pydantic_error_handler(
    request: Request, exc: ValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "detail": "Invalid request body",
            "errors": [
                {"loc": list(e.get("loc", [])), "msg": e.get("msg", "")}
                for e in exc.errors()
            ],
        },
    )


@app.exception_handler(Exception)
async def _generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Never leak stack traces or secrets.
    logger.exception("Unhandled error while processing %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


def _build_fallback_response(
    req: OptimizeRequest,
    directives: List[Dict[str, Any]],
    reason: str,
) -> Dict[str, Any]:
    """All-idle safe plan wrapped in the full response schema."""
    hours_raw = [h.model_dump() for h in req.hours]
    battery_raw = req.battery.model_dump()
    fallback = build_safe_plan(hours_raw, battery_raw, directives)

    return {
        "scenario_id": req.scenario_id,
        "directive_interpretation": directives,
        "hourly_plan": fallback["hourly_plan"],
        "total_grid_kwh": fallback["total_grid_kwh"],
        "total_cost_bdt": fallback["total_cost_bdt"],
        "peak_grid_kwh": fallback["peak_grid_kwh"],
        "plan_summary": (
            "Fallback all-idle plan used because the primary optimizer or "
            f"self-check failed ({reason}). Battery is untouched, solar is used "
            "up to demand, remaining demand supplied by grid."
        ),
    }


def _build_llm_noop_directives(num_notes: int) -> List[Dict[str, Any]]:
    """When the LLM is unreachable, return N no_op entries."""
    return [
        {
            "note_index": i,
            "applies": False,
            "directive_type": "no_op",
            "structured_adjustment": None,
            "explanation": (
                "LLM interpretation unavailable; note treated as no_op so the "
                "schedule remains valid."
            ),
        }
        for i in range(num_notes)
    ]


@app.post("/optimize-energy", response_model=OptimizeResponse)
async def optimize_energy(req: OptimizeRequest) -> Any:
    # ------------------------------------------------------------------
    # Step 2: LLM interpretation
    # ------------------------------------------------------------------
    raw_directives: List[Dict[str, Any]] = []
    try:
        raw_directives = interpret_notes(
            operator_notes=req.operator_notes,
            battery_capacity_kwh=float(req.battery.capacity_kwh),
        )
    except Exception as exc:  # noqa: BLE001 - never crash on LLM failure
        logger.warning("LLM call raised %s; degrading to no_op list", type(exc).__name__)

    # ------------------------------------------------------------------
    # Step 3: Guardrail repair
    # ------------------------------------------------------------------
    try:
        directives = guardrail_directives(
            raw_directives=raw_directives,
            num_notes=len(req.operator_notes),
            battery_capacity_kwh=float(req.battery.capacity_kwh),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Guardrails failed: %s", type(exc).__name__)
        directives = _build_llm_noop_directives(len(req.operator_notes))

    # ------------------------------------------------------------------
    # Step 4: LP optimizer
    # ------------------------------------------------------------------
    hours_raw = [h.model_dump() for h in req.hours]
    battery_raw = req.battery.model_dump()

    optimized: Dict[str, Any] | None = None
    try:
        optimized = optimize(hours_raw, battery_raw, directives)
    except Exception as exc:  # noqa: BLE001
        logger.error("Optimizer raised %s", type(exc).__name__)
        optimized = None

    if optimized is None:
        logger.warning("Optimizer returned None; using fallback plan")
        response_payload = _build_fallback_response(req, directives, "LP infeasible")
        return JSONResponse(status_code=200, content=response_payload)

    # Build a candidate response dict for validation.
    candidate: Dict[str, Any] = {
        "scenario_id": req.scenario_id,
        "directive_interpretation": directives,
        "hourly_plan": optimized["hourly_plan"],
        "total_grid_kwh": optimized["total_grid_kwh"],
        "total_cost_bdt": optimized["total_cost_bdt"],
        "peak_grid_kwh": optimized["peak_grid_kwh"],
        "plan_summary": (
            "LP-optimized 24-hour schedule. Directives were interpreted by the "
            "LLM, repaired by deterministic guardrails, then applied to the "
            "linear program to minimize grid cost subject to energy, battery, "
            "and operator-directive constraints."
        ),
    }

    # ------------------------------------------------------------------
    # Step 5: Self-validate (judge mirror)
    # ------------------------------------------------------------------
    try:
        violations = validate_response(
            request_hours=hours_raw,
            battery=battery_raw,
            directives=directives,
            response=candidate,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Validator raised %s", type(exc).__name__)
        violations = ["validator_error"]

    if violations:
        logger.warning(
            "Self-check failed with %d violation(s); using fallback plan. First: %s",
            len(violations),
            violations[0],
        )
        response_payload = _build_fallback_response(
            req, directives, "self-check failed"
        )
        return JSONResponse(status_code=200, content=response_payload)

    # ------------------------------------------------------------------
    # Step 6: Return the validated response
    # ------------------------------------------------------------------
    return JSONResponse(status_code=200, content=candidate)


__all__ = ["app"]