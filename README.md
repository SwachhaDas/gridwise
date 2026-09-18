# GridWise — BUP CSE Fest 2026 Preliminary

LLM-assisted smart-campus energy optimization service for the
[BUP CSE Fest 2026 GridWise preliminary](https://bup.edu.bd).

## Architecture

```
                ┌─────────────────────────────┐
                │  POST /optimize-energy      │
                │  (FastAPI, app/main.py)     │
                └──────────────┬──────────────┘
                               │
        ┌──────────────────────┼──────────────────────┐
        ▼                      ▼                      ▼
   ┌─────────┐          ┌─────────────┐        ┌────────────┐
   │ app/    │          │ app/        │        │ app/       │
   │ llm.py  │  --->    │ guardrails. │  --->  │ optimizer. │
   │         │          │ py          │        │ py         │
   │  LLM    │          │             │        │            │
   │ interp  │          │  repair +   │        │  PuLP LP   │
   │         │          │  validate   │        │  solve     │
   └─────────┘          └─────────────┘        └──────┬─────┘
                                                       │
                                          ┌────────────▼─────────────┐
                                          │  app/validator.py        │
                                          │  self-check (judge       │
                                          │  mirror)                 │
                                          └────────────┬─────────────┘
                                                       │
                                          ┌────────────▼─────────────┐
                                          │  app/fallback.py         │
                                          │  (all-idle safe plan if  │
                                          │  LP or check fails)      │
                                          └──────────────────────────┘
```

**Pipeline:**

1. **LLM interpreter** (`app/llm.py`) — a single chat-completion call converts
   every operator note into a structured directive. Uses an OpenAI-compatible
   provider (Groq, DeepSeek, OpenAI, Gemini via OpenRouter, etc).
2. **Deterministic guardrails** (`app/guardrails.py`) — repairs malformed LLM
   output (unknown types → `no_op`, dedupes/clamps hours, clamps factors,
   reindexes note order, fills missing entries).
3. **LP optimizer** (`app/optimizer.py`) — PuLP + CBC linear program with
   variables `g[h]`, `s[h]`, `c[h]`, `d[h]`, `E[h]`. Objective: minimize grid
   cost. Applies directives as hard constraints.
4. **Post-processing** — nets simultaneous charge/discharge, recomputes grid
   from balance, recomputes `E[h]` cumulatively.
5. **Self-check** (`app/validator.py`) — replays the returned schedule exactly
   like the judge would. On any violation, substitutes the fallback plan.

## Endpoints

| Method | Path               | Description                                        |
| ------ | ------------------ | -------------------------------------------------- |
| GET    | `/health`          | Returns `{"status": "ok"}`                         |
| POST   | `/optimize-energy` | Interprets operator notes and returns 24h schedule |

## Environment Variables

| Variable             | Required | Default                        | Notes                                                       |
| -------------------- | -------- | ------------------------------ | ----------------------------------------------------------- |
| `DEEPSEEK_API_KEY`   | ✅       | —                              | API key for the LLM provider                                |
| `DEEPSEEK_BASE_URL`  | ❌       | `https://api.deepseek.com`     | OpenAI-compatible base URL (e.g. Groq, OpenRouter)          |
| `DEEPSEEK_MODEL`     | ❌       | `deepseek-chat`                | Model identifier for the provider                           |
| `PORT`               | ❌       | `8000`                         | HTTP port to bind                                           |
| `LOG_LEVEL`          | ❌       | `INFO`                         | Python logging level                                        |
| `LLM_TIMEOUT_S`      | ❌       | `20`                           | Per-request LLM timeout in seconds                          |

### Example `.env` (Groq)

```
DEEPSEEK_API_KEY=gsk_xxx
DEEPSEEK_BASE_URL=https://api.groq.com/openai/v1
DEEPSEEK_MODEL=openai/gpt-oss-20b
PORT=8000
```

### Example `.env` (DeepSeek paid)

```
DEEPSEEK_API_KEY=sk-xxx
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
PORT=8000
```

## Local Run

```bash
# 1. Clone and enter the repo
git clone <your-repo-url> gridwise
cd gridwise

# 2. Create and activate a virtual environment
python -m venv venv
# Windows Git Bash:
source venv/Scripts/activate
# Linux / macOS:
# source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# edit .env and set DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL

# 5. Start the server
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 6. In another terminal, run the health check
curl http://localhost:8000/health
# -> {"status":"ok"}
```

## Sample Curl

### Health

```bash
curl http://localhost:8000/health
```

### Optimize

```bash
curl -X POST http://localhost:8000/optimize-energy \
  -H "Content-Type: application/json" \
  -d '{
    "scenario_id": "GRID-101",
    "operator_notes": [
      "Solar output will drop to about 20% from 1 PM to 3 PM.",
      "Do not charge the battery between 2 PM and 4 PM.",
      "The cafeteria menu changes tomorrow."
    ],
    "hours": [
      {"hour": 0, "demand_kwh": 180, "solar_kwh": 0,  "tariff_bdt_per_kwh": 7},
      {"hour": 1, "demand_kwh": 175, "solar_kwh": 0,  "tariff_bdt_per_kwh": 7},
      {"hour": 2, "demand_kwh": 170, "solar_kwh": 0,  "tariff_bdt_per_kwh": 6},
      {"hour": 3, "demand_kwh": 170, "solar_kwh": 0,  "tariff_bdt_per_kwh": 6},
      {"hour": 4, "demand_kwh": 175, "solar_kwh": 0,  "tariff_bdt_per_kwh": 6},
      {"hour": 5, "demand_kwh": 185, "solar_kwh": 5,  "tariff_bdt_per_kwh": 7},
      {"hour": 6, "demand_kwh": 200, "solar_kwh": 15, "tariff_bdt_per_kwh": 9},
      {"hour": 7, "demand_kwh": 220, "solar_kwh": 40, "tariff_bdt_per_kwh": 11},
      {"hour": 8, "demand_kwh": 240, "solar_kwh": 80, "tariff_bdt_per_kwh": 13},
      {"hour": 9, "demand_kwh": 255, "solar_kwh": 130,"tariff_bdt_per_kwh": 15},
      {"hour": 10,"demand_kwh": 265, "solar_kwh": 180,"tariff_bdt_per_kwh": 16},
      {"hour": 11,"demand_kwh": 275, "solar_kwh": 210,"tariff_bdt_per_kwh": 16},
      {"hour": 12,"demand_kwh": 280, "solar_kwh": 230,"tariff_bdt_per_kwh": 15},
      {"hour": 13,"demand_kwh": 275, "solar_kwh": 220,"tariff_bdt_per_kwh": 14},
      {"hour": 14,"demand_kwh": 265, "solar_kwh": 180,"tariff_bdt_per_kwh": 13},
      {"hour": 15,"demand_kwh": 255, "solar_kwh": 130,"tariff_bdt_per_kwh": 14},
      {"hour": 16,"demand_kwh": 260, "solar_kwh": 70, "tariff_bdt_per_kwh": 18},
      {"hour": 17,"demand_kwh": 275, "solar_kwh": 20, "tariff_bdt_per_kwh": 22},
      {"hour": 18,"demand_kwh": 300, "solar_kwh": 0,  "tariff_bdt_per_kwh": 28},
      {"hour": 19,"demand_kwh": 315, "solar_kwh": 0,  "tariff_bdt_per_kwh": 30},
      {"hour": 20,"demand_kwh": 305, "solar_kwh": 0,  "tariff_bdt_per_kwh": 26},
      {"hour": 21,"demand_kwh": 270, "solar_kwh": 0,  "tariff_bdt_per_kwh": 18},
      {"hour": 22,"demand_kwh": 220, "solar_kwh": 0,  "tariff_bdt_per_kwh": 10},
      {"hour": 23,"demand_kwh": 190, "solar_kwh": 0,  "tariff_bdt_per_kwh": 8}
    ],
    "battery": {
      "capacity_kwh": 500,
      "initial_energy_kwh": 200,
      "minimum_energy_kwh": 50,
      "max_charge_kwh_per_hour": 100,
      "max_discharge_kwh_per_hour": 100
    }
  }'
```

## Running the Sample Suite

With the server running locally:

```bash
python tests/run_samples.py http://localhost:8000
```

Expected output: a PASS/FAIL table with recalculated cost per sample. All 10
samples should PASS.

## Docker

### Build

```bash
docker build -t gridwise:1.0.0 .
```

### Run

```bash
docker run --rm -p 8000:8000 --env-file .env gridwise:1.0.0
```

Then:

```bash
curl http://localhost:8000/health
# -> {"status":"ok"}
```

The image does **not** bake in secrets — pass them at runtime via
`--env-file` or `-e`.

## Deployment (Render / Railway / Fly.io)

1. Push the repo to GitHub (keep `.env` out of the repo — `.gitignore` already
   excludes it).
2. Create a new Web Service and point it at the repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
5. Set env vars: `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL`,
   and optionally `PORT`.
6. Deploy and confirm `GET /health` returns `{"status":"ok"}`.

## Testing

```bash
pytest -q
```

(If no tests are present, this will be a no-op.)

## Known Limitations

- The LLM provider must be reachable during the entire evaluation window.
  If the provider is down or rate-limits the key, the service returns a
  valid fallback (all-idle) plan rather than failing the request.
- The bundled CBC solver is sufficient for 24-hour LPs but may be slow on
  extremely large instances; the current problem size is fixed at 24 hours.
- The service assumes the harness supplies synthetic data only (no live
  utility/billing/personal data).

## Third-Party Credits

- **FastAPI** — web framework
- **Pydantic v2** — data validation
- **PuLP + CBC** — linear programming solver
- **OpenAI Python SDK** — OpenAI-compatible client used for Groq / DeepSeek /
  OpenRouter / Gemini endpoints
- **python-dotenv** — `.env` loading
- **httpx** — HTTP client used by the sample runner

## License

MIT (or as required by the event organizers).