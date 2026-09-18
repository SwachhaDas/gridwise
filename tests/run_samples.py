"""
Run all sample scenarios against a running GridWise server.

Usage:
    python tests/run_samples.py [BASE_URL]

    BASE_URL defaults to http://localhost:8000

Prints a PASS/FAIL table with total cost for each sample.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import httpx

# Allow running as `python tests/run_samples.py`
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.validator import validate_response  # noqa: E402


SAMPLES_PATH = _HERE / "samples.json"


def load_samples() -> List[Dict[str, Any]]:
    with SAMPLES_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data["samples"]


def run_one(client: httpx.Client, base_url: str, sample: Dict[str, Any]) -> Dict[str, Any]:
    name = sample["name"]
    payload = sample["input"]

    result = {
        "name": name,
        "http_ok": False,
        "http_status": None,
        "validator_violations": [],
        "total_cost": None,
        "directives": None,
        "error": None,
    }

    try:
        resp = client.post(f"{base_url}/optimize-energy", json=payload, timeout=60.0)
        result["http_status"] = resp.status_code
        if resp.status_code != 200:
            result["error"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
            return result

        result["http_ok"] = True
        response_json = resp.json()

        # directive summary
        dirs = response_json.get("directive_interpretation") or []
        result["directives"] = [
            f"{d.get('directive_type')}"
            + (
                f"({len(d['structured_adjustment'].get('hours', []))}h)"
                if d.get("structured_adjustment")
                else ""
            )
            for d in dirs
        ]
        result["total_cost"] = response_json.get("total_cost_bdt")

        # Self-validate against judge mirror
        violations = validate_response(
            request_hours=payload["hours"],
            battery=payload["battery"],
            directives=dirs,
            response=response_json,
        )
        result["validator_violations"] = violations

    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"

    return result


def main() -> int:
    base_url = sys.argv[1] if len(sys.argv) > 1 else os.getenv(
        "GRIDWISE_URL", "http://localhost:8000"
    )
    base_url = base_url.rstrip("/")

    print(f"GridWise sample test runner")
    print(f"Target: {base_url}")
    print(f"Samples: {SAMPLES_PATH}")
    print("-" * 100)

    samples = load_samples()

    # Quick health check
    try:
        with httpx.Client() as client:
            h = client.get(f"{base_url}/health", timeout=10.0)
            if h.status_code != 200 or h.json().get("status") != "ok":
                print(f"FAIL: /health did not return ok (status={h.status_code})")
                return 2
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: cannot reach {base_url}/health -> {type(exc).__name__}: {exc}")
        return 2

    print(f"{'#':<3} {'Result':<8} {'Cost':>12}  {'Directives':<40} Name")
    print("-" * 100)

    passed = 0
    failed = 0
    with httpx.Client() as client:
        for i, sample in enumerate(samples, 1):
            r = run_one(client, base_url, sample)

            if not r["http_ok"]:
                verdict = "FAIL"
                cost_str = "-"
                dirs_str = f"HTTP {r['http_status']} err"
                failed += 1
            elif r["validator_violations"]:
                verdict = "FAIL"
                cost_str = f"{r['total_cost']:.2f}" if r["total_cost"] is not None else "-"
                dirs_str = f"{len(r['validator_violations'])} violation(s)"
                failed += 1
            else:
                verdict = "PASS"
                cost_str = f"{r['total_cost']:.2f}" if r["total_cost"] is not None else "-"
                dirs_str = ", ".join(r["directives"] or [])[:40]
                passed += 1

            name = r["name"][:60]
            print(f"{i:<3} {verdict:<8} {cost_str:>12}  {dirs_str:<40} {name}")

            if r["error"]:
                print(f"      error: {r['error']}")
            if r["validator_violations"]:
                for v in r["validator_violations"][:3]:
                    print(f"      violation: {v}")

    print("-" * 100)
    print(f"Summary: {passed} passed, {failed} failed, {len(samples)} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())