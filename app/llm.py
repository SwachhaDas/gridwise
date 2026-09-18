"""
DeepSeek / OpenRouter LLM client for operator-note interpretation.

Responsibilities:
- Load .env BEFORE reading any env vars, so DEEPSEEK_BASE_URL and
  DEEPSEEK_MODEL overrides are always honoured.
- Build a strong system prompt with the 6 supported directive schemas.
- Include few-shot examples covering paraphrase variants.
- Call the OpenAI-compatible chat completions endpoint ONCE for all notes.
- Extract JSON robustly (strip ``` fences, find first {...} block).
- Retry once on transient failures with a short timeout.
- Never log the API key or raw provider responses containing secrets.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from openai import OpenAI

# ---------------------------------------------------------------------------
# CRITICAL: load .env BEFORE reading any environment variables.
#
# If this runs after module-level os.getenv() calls, the DEEPSEEK_BASE_URL
# and DEEPSEEK_MODEL overrides silently fall back to their defaults
# (api.deepseek.com). That is exactly the kind of bug that leads to
# 401 Unauthorized against the wrong provider at runtime.
# ---------------------------------------------------------------------------

load_dotenv()

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config (resolved AFTER load_dotenv)
# ---------------------------------------------------------------------------

DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEFAULT_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "20"))

# Log resolved config once at import time (NEVER the key).
# Uses WARNING level so the message survives even when logging.basicConfig()
# has not yet been called by app.main (import-time logging quirk).
logger.warning(
    "LLM config resolved: base_url=%s model=%s timeout=%ss",
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    DEFAULT_TIMEOUT_S,
)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = r"""You are a strict information-extraction engine for a smart-campus
energy optimization system called GridWise.

You will be given 1 to 3 natural-language operator notes describing temporary
operating conditions affecting a 24-hour energy schedule. Your job is to
convert EVERY note into exactly one structured directive.

==================== OUTPUT FORMAT ====================
Return ONLY a JSON object with this exact shape:

{
  "directives": [
    {
      "note_index": 0,
      "applies": true,
      "directive_type": "<one of the 6 types below>",
      "structured_adjustment": { ... } or null,
      "explanation": "short human-readable reason"
    },
    ...
  ]
}

The "directives" array MUST contain exactly one entry for EVERY note,
in note_index order: 0, 1, 2, ... N-1.
No missing entries. No extra entries. No duplicate note_index values.

==================== THE 6 ALLOWED DIRECTIVE TYPES ====================
Only these six are allowed. Never invent new types.

1. solar_reduction
   Meaning: usable solar is reduced during specific hours.
   structured_adjustment: {"hours": [int,...], "factor": number}
   factor = USABLE FRACTION REMAINING (not the reduction amount).
     - "drops to about 20%"          -> factor = 0.2
     - "80% reduction"               -> factor = 0.2
     - "one-fifth of normal"         -> factor = 0.2
     - "half of forecast"            -> factor = 0.5
     - "leaves roughly 25%"          -> factor = 0.25
   Hours must be unique integers 0..23 in ascending order.

2. minimum_battery_reserve
   Meaning: battery energy must stay at or above a required level.
   structured_adjustment: {"hours": [int,...], "minimum_energy_kwh": number}
   If the note expresses a PERCENTAGE of capacity, you must convert it
   using the battery_capacity_kwh supplied in the user message.
     - "keep at least 50% of capacity from 6 PM until 9 PM"
       with capacity=200 -> minimum_energy_kwh = 100, hours = [18,19,20]
   Hours must be unique integers 0..23 in ascending order.

3. no_charge_window
   Meaning: battery charging is disabled during specific hours.
   structured_adjustment: {"hours": [int,...]}

4. no_discharge_window
   Meaning: battery discharging is disabled during specific hours.
   structured_adjustment: {"hours": [int,...]}

5. max_grid_window
   Meaning: grid import may not exceed a stated amount during specific hours.
   structured_adjustment: {"hours": [int,...], "max_grid_kwh": number}

6. no_op
   Meaning: the note does NOT affect the current 24-hour energy schedule.
   structured_adjustment: null
   applies MUST be false for no_op.
   applies MUST be true for every other directive.

==================== TIME WINDOW RULES ====================
- Time windows are START-INCLUSIVE and END-EXCLUSIVE.
  "1 PM to 3 PM"      -> hours [13, 14]
  "6 PM until 9 PM"   -> hours [18, 19, 20]
  "2 AM until 5 AM"   -> hours [2, 3, 4]
  "from 11 AM to 1 PM" -> hours [11, 12]
- Use whole-hour integers 0..23 (midnight = 0, noon = 12, 1 PM = 13,
  6 PM = 18, 11 PM = 23). No minutes, no decimals in hours.
- Hours must be unique and sorted ascending.

==================== DISTRACTOR NOTES ====================
Notes about the following are almost always no_op:
- cafeteria menu changes
- library / exam / registration schedules
- sports office deadlines
- cleaning staff, wifi outages, seminar bookings
- anything not affecting grid / solar / battery in the next 24 hours.
Mark them applies = false, directive_type = "no_op", structured_adjustment = null.

==================== HARD CONSTRAINTS ====================
- NEVER invent demand, solar, tariff, battery limits, or directive types.
- NEVER return prose outside the JSON object.
- NEVER wrap the JSON in markdown unless explicitly asked; if you do,
  only use ```json fences around a single JSON object.
- If a note is ambiguous but clearly energy-relevant, choose the closest
  supported directive rather than no_op.
- If a note is clearly not energy-relevant, use no_op.

==================== FEW-SHOT EXAMPLES ====================

Example A (paraphrase: "13:00 to 15:00"):
Notes:
  0: "PV production will drop to about 20% between 13:00 and 15:00."
Response:
{"directives":[{"note_index":0,"applies":true,"directive_type":"solar_reduction","structured_adjustment":{"hours":[13,14],"factor":0.2},"explanation":"Solar output limited to 20% during the stated window."}]}

Example B (paraphrase: "one until three"):
Notes:
  0: "Panel washing from one until three will leave roughly one-fifth of normal solar output."
Response:
{"directives":[{"note_index":0,"applies":true,"directive_type":"solar_reduction","structured_adjustment":{"hours":[13,14],"factor":0.2},"explanation":"One-fifth remaining solar during panel washing."}]}

Example C (paraphrase: "an 80% reduction"):
Notes:
  0: "Expect an 80% reduction in rooftop solar during the 1-3 PM maintenance window."
Response:
{"directives":[{"note_index":0,"applies":true,"directive_type":"solar_reduction","structured_adjustment":{"hours":[13,14],"factor":0.2},"explanation":"80% reduction leaves 20% usable solar."}]}

Example D (distractor + relevant):
Notes:
  0: "Facilities will wash the rooftop solar panels from noon until 2 PM. During cleaning, usable solar should be treated as roughly 25% of the forecast."
  1: "The sports office moved next month's registration deadline."
Response:
{"directives":[{"note_index":0,"applies":true,"directive_type":"solar_reduction","structured_adjustment":{"hours":[12,13],"factor":0.25},"explanation":"Usable solar reduced to 25% during panel cleaning."},{"note_index":1,"applies":false,"directive_type":"no_op","structured_adjustment":null,"explanation":"This note does not affect today's 24-hour energy schedule."}]}

Example E (reserve as percentage of capacity):
Notes:
  0: "Keep at least 50% of the battery capacity stored in the battery from 6 PM until 9 PM for emergency operations."
Battery capacity supplied in user message: 200 kWh.
Response:
{"directives":[{"note_index":0,"applies":true,"directive_type":"minimum_battery_reserve","structured_adjustment":{"hours":[18,19,20],"minimum_energy_kwh":100},"explanation":"Half of 200 kWh capacity is 100 kWh, required for the stated window."}]}

Example F (no-charge + no-discharge + distractor):
Notes:
  0: "Battery charging is disabled from 11 AM until 1 PM while technicians inspect the charger."
  1: "Do not discharge the battery from 5 PM until 7 PM during relay testing."
  2: "A seminar room booking was moved to next week."
Response:
{"directives":[{"note_index":0,"applies":true,"directive_type":"no_charge_window","structured_adjustment":{"hours":[11,12]},"explanation":"Battery charging disabled during charger inspection."},{"note_index":1,"applies":true,"directive_type":"no_discharge_window","structured_adjustment":{"hours":[17,18]},"explanation":"Battery discharge disabled during relay testing."},{"note_index":2,"applies":false,"directive_type":"no_op","structured_adjustment":null,"explanation":"Seminar booking does not affect today's energy schedule."}]}

Example G (grid cap):
Notes:
  0: "From 6 PM until 9 PM, campus grid import must not exceed 155 kWh in any hour because the feeder is operating under a temporary limit."
Response:
{"directives":[{"note_index":0,"applies":true,"directive_type":"max_grid_window","structured_adjustment":{"hours":[18,19,20],"max_grid_kwh":155},"explanation":"Grid import capped at 155 kWh per hour in feeder-restriction window."}]}

==================== FINAL REMINDER ====================
Return exactly one JSON object. No prose. No markdown headings.
Every note gets exactly one directive entry, in note_index order.
"""


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _strip_code_fences(text: str) -> str:
    """If the model wrapped JSON in ``` fences, unwrap it."""
    match = _JSON_FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


def _extract_first_json_object(text: str) -> Optional[Dict[str, Any]]:
    """
    Find and parse the first top-level JSON object in `text`.

    Handles:
    - plain JSON
    - ```json ... ``` fences
    - leading/trailing prose with an embedded JSON object
    """
    if not text:
        return None

    cleaned = _strip_code_fences(text)

    # Fast path: already valid JSON
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Slow path: scan for the first balanced {...} block
    start = cleaned.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(cleaned)):
            ch = cleaned[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = cleaned[start : i + 1]
                    try:
                        parsed = json.loads(candidate)
                        if isinstance(parsed, dict):
                            return parsed
                    except json.JSONDecodeError:
                        pass
                    break
        start = cleaned.find("{", start + 1)

    return None


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------


def _get_client() -> OpenAI:
    # Read the key at call time so .env reloads in dev still work.
    load_dotenv(override=False)
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY environment variable is not set")

    # Re-read the base URL here too, in case .env changed since import.
    base_url = os.getenv("DEEPSEEK_BASE_URL", DEEPSEEK_BASE_URL)

    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=DEFAULT_TIMEOUT_S,
    )


def _get_model() -> str:
    load_dotenv(override=False)
    return os.getenv("DEEPSEEK_MODEL", DEEPSEEK_MODEL)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def interpret_notes(
    operator_notes: List[str],
    battery_capacity_kwh: float,
) -> List[Dict[str, Any]]:
    """
    Call the LLM ONCE with all notes. Return a list of raw directive dicts
    (not yet guardrailed). On any failure returns an empty list so the caller
    can degrade gracefully.
    """
    if not operator_notes:
        return []

    user_payload = {
        "operator_notes": [
            {"note_index": i, "text": note} for i, note in enumerate(operator_notes)
        ],
        "battery_capacity_kwh": battery_capacity_kwh,
        "supported_directive_types": [
            "solar_reduction",
            "minimum_battery_reserve",
            "no_charge_window",
            "no_discharge_window",
            "max_grid_window",
            "no_op",
        ],
        "reminder": (
            "Return exactly one directive per note, in note_index order. "
            "Use whole-hour integers 0..23. Windows are start-inclusive, end-exclusive."
        ),
    }

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]

    last_error: Optional[Exception] = None
    for attempt in range(2):  # one retry on failure
        try:
            client = _get_client()
            model = _get_model()
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0,
                response_format={"type": "json_object"},
                timeout=DEFAULT_TIMEOUT_S,
            )
            content = response.choices[0].message.content or ""
            parsed = _extract_first_json_object(content)
            if not parsed:
                raise ValueError("LLM response did not contain a valid JSON object")
            directives = parsed.get("directives")
            if not isinstance(directives, list):
                raise ValueError("LLM JSON missing 'directives' list")
            logger.info(
                "LLM returned %d directive(s) for %d note(s) using model=%s",
                len(directives),
                len(operator_notes),
                model,
            )
            return directives
        except Exception as exc:  # noqa: BLE001 - we intentionally degrade
            last_error = exc
            logger.warning(
                "LLM call attempt %d failed: %s", attempt + 1, type(exc).__name__
            )

    logger.error(
        "LLM interpretation failed after retries (%s). Degrading to no_op list.",
        type(last_error).__name__ if last_error else "unknown",
    )
    return []


__all__ = ["interpret_notes", "SYSTEM_PROMPT"]