# One Copy — Did My Submission Go Through?

> **When the confirmation disappears, One Copy tests which recovery flow gets your work submitted exactly once.**

**Team Spidey Sense** — Pranav Moorthy IV & Antony Selva Jasfer  
**Hackathon Track** — Toggle Wars Track 01: Test Something Real

- **Live App (Vercel + Render + Neon + Flagsmith Cloud)**: https://web-two-sigma-w7mbp23lzt.vercel.app/
- **Demo Video (2:30)**: `[Demo Video Link — Coming Soon]`
- **Flagsmith Production Experiment (`919`)**: https://app.flagsmith.com/project/56524/environment/kVfxWTvFU2vVKTnZrYFye6/experiments/919
- **Hypothesis**: Prompting users to check submission status before retrying increases 60-second single-entry recovery (`single_entry_receipt_60s`) and reduces duplicate submissions (`duplicate_entry_created`) when HTTP replies or requests are lost.

---

## Why This Matters

Every hackathon, scholarship, and grant portal has the same 11:59 PM failure mode: you click **Submit**, the connection hangs or drops, and you have no idea whether your entry was saved or lost.

### Why not just use a backend idempotency key?

1. **Legacy portals lack idempotency keys**: Many university, government, and contest forms commit on `POST` without a client-generated idempotency token — if the HTTP reply drops (`reply_lost`), a manual resubmit writes a second copy.
2. **Panicked users open new tabs or refresh**: Even when a backend deduplicates identical button-click retries, users who lose confirmation routinely refresh the page, open a second tab, or re-fill the form — generating a brand-new request/action ID (`final_entry_count = 2`).
3. **Reusable recovery UX**: We packaged the recovery experience as a drop-in React component ([`SubmissionRecovery.tsx`](web/src/components/SubmissionRecovery.tsx)) so any submission flow can test **Check submission status first** (`check-status-first`) against **Retry immediately** (`control`) with real server-enforced outcomes.

---

## Architecture & Experimental Integrity

```text
Browser (React + TS + Vite on Vercel)
  │  relative /api/* (credentials: "include", no Flagsmith keys in client bundle)
  ▼
Vercel Rewrite (/api/:path* -> https://one-copy.onrender.com/api/:path*)
  │
  ▼
FastAPI Backend (redirect_slashes=False on Render)
  ├── Signed HttpOnly Cookie (oc_visitor) + Origin Check (APP_ORIGIN) + Cache-Control: no-store
  ├── Flagsmith Python SDK (flagsmith==6.7.0, local eval + EventProcessorConfig)
  └── Neon PostgreSQL (trials, entries, actions, allocation_counters, event_ledger)
```

### Integrity Safeguards

- **Paired Failure Balancing (`HMAC-SHA256`)**: In [`api/app/allocation.py`](api/app/allocation.py), each trial is assigned `reply_lost` or `request_lost` deterministically in balanced pairs via `HMAC-SHA256(BALANCE_SECRET, f"{experiment}:{pair_index}")` with transactional `FOR UPDATE` row locks in `allocation_counters`. Every consecutive pair `(2k, 2k+1)` per arm receives one `reply_lost` and one `request_lost` in identical order across both arms (`75/75` in `control`, `76/75` in `check-status-first`).
- **Stable Identity & First-Party Cookie Proxy**: `POST /api/session` issues an HMAC-signed `HttpOnly` `oc_visitor` UUID cookie (`SameSite=Lax`, `Path=/`, `Secure` in production, 30-day TTL) proxied first-party through Vercel's `/api/*` rewrite. FastAPI runs with `FastAPI(redirect_slashes=False)` so `307` redirects never break proxied cookies. Exposures and conversion events join on `str(visitor_id)`.
- **Double-Click & Refresh Idempotency**: Transport hiccups reuse the same `actionId` UUID (`actions` primary key `(trial_id, action_id)`), while `event_ledger` primary key `(trial_id, event_name)` guarantees `single_entry_receipt_60s` and `duplicate_entry_created` never fire twice on receipt page refresh.
- **Visible-Tab Receipt Acknowledgment**: [`SubmissionRecovery.tsx`](web/src/components/SubmissionRecovery.tsx) gates `POST /api/receipts/{code}/displayed` behind both `IntersectionObserver` and `document.visibilityState === "visible"`, firing only when the receipt is actually visible in an active tab and within the server-enforced 60-second deadline.
- **Real `flagsmith==6.7.0` `EventProcessorConfig` Finding**: During QA smoke testing, setting `EventProcessorConfig(max_buffer_items=1)` caused `flagsmith==6.7.0` to log `Event buffer was full: dropped the 59 oldest events`, because the SDK uses `max_buffer_items` as both the flush threshold and the hard ring-buffer cap (`_drop_oldest_events`). Updating [`api/app/flagsmith_service.py`](api/app/flagsmith_service.py) to `EventProcessorConfig(max_buffer_items=100, flush_interval_seconds=1.0)` plus explicit `flush()` and FastAPI lifespan `close()` delivered **554/554** production events with **0 dropped**.

---

## Flagsmith Setup Reproduction (CLI v2)

Reproduce the multivariate flag, occurrence metrics, and 50/50 split experiment in Flagsmith Cloud (`flagsmith` CLI v2):

```powershell
# 1. Authenticate & bind environment
flagsmith login
flagsmith init --project <project-id> --environment <env-key>

# 2. Verify managed ClickHouse warehouse is connected
flagsmith api api/v1/environments/<env-key>/warehouse-connections/

# 3. Create multivariate flag with keyed variation (keys are required for exposures)
flagsmith feature create submission_recovery --value retry --enabled --description "Recovery flow experiment: retry vs check-status-first"
flagsmith feature variant add submission_recovery --value check_status --key check-status-first --weight 0
flagsmith feature get submission_recovery --json

# 4. Create occurrence metrics (case-sensitive event names)
'{"name":"Single Entry Receipt 60s","aggregation":"occurrence","direction":"up","definition":{"version":1,"event":"single_entry_receipt_60s"}}' | flagsmith api api/v1/environments/<env-key>/experiment-metrics/ -X POST --input -
'{"name":"Duplicate Entry Created","aggregation":"occurrence","direction":"down","definition":{"version":1,"event":"duplicate_entry_created"}}' | flagsmith api api/v1/environments/<env-key>/experiment-metrics/ -X POST --input -

# 5. Create 100% rollout 50/50 experiment & start it
# (Replace <feature-id>, <metric-up-id>, <metric-down-id>, and <variant-id> from steps 3-4)
'{"feature":<feature-id>,"name":"Submission Recovery: Check Status First vs Retry","hypothesis":"Prompting users to check submission status before retrying increases 60s single-entry recovery and reduces duplicate entries when replies or requests are lost.","metrics":[{"metric":<metric-up-id>,"expected_direction":"increase"},{"metric":<metric-down-id>,"expected_direction":"decrease"}],"experiment_rollout":{"enabled":true,"rollout_percentage":100,"feature_state_value":{"type":"string","value":"retry"},"multivariate_feature_state_values":[{"multivariate_feature_option":<variant-id>,"percentage_allocation":50}]}}' | flagsmith api api/v1/environments/<env-key>/experiments/ -X POST --input -
flagsmith api api/v1/environments/<env-key>/experiments/<experiment-id>/start/ -X POST
```

---

## Verified Results & Ground-Truth Reconciliation

Snapshot captured at **`2026-11-27T05:37:04Z`** from Flagsmith Production Experiment `919` (managed ClickHouse warehouse `12902`, `last_error_at: null`) and `GET https://web-two-sigma-w7mbp23lzt.vercel.app/api/stats/summary` (Neon PostgreSQL):

### Flagsmith Experiment `919` vs Neon Postgres

| Metric / Count | Arm | Flagsmith (`919`) | Neon Postgres (`/api/stats/summary`) | Match |
| --- | --- | ---: | ---: | --- |
| **Exposures (`n`)** | `control` (`retry`) | **150** | **150** (`75 reply_lost` + `75 request_lost`) | Exact |
| **Exposures (`n`)** | `check-status-first` (`check_status`) | **151** | **151** (`76 reply_lost` + `75 request_lost`) | Exact |
| **Primary (`single_entry_receipt_60s`)** | `control` | **63** (`42.0%`) | **63** (`0` + `63`) | Exact |
| **Primary (`single_entry_receipt_60s`)** | `check-status-first` | **123** (`81.5%`) | **123** (`67` + `56`) | Exact |
| **Guardrail (`duplicate_entry_created`)** | `control` | **67** (`44.7%`) | **67** (`67` + `0`) | Exact |
| **Guardrail (`duplicate_entry_created`)** | `check-status-first` | **0** (`0.0%`) | **0** (`0` + `0`) | Exact |
| **Total Warehouse Events** | All live | **554** (`301 + 186 + 67`) | **554** | **0 dropped** |

### Statistical Lift & Latency Summary

- **Flagsmith Bayesian Inference (`metric_id: 969`)**:
  - **Relative Lift**: **`+93.95%`** (`0.9395`)
  - **95% Credible Interval**: **`[+54.5%, +133.4%]`** (`[0.5447, 1.3342]`)
  - **Chance to Win**: **`99.9998%`** (`0.99999845`)
- **Neon Postgres Overall (`liveTotalExposed = 301`, `practice = 1`)**:
  - **Overall Primary Rate**: `61.79%`
  - **Overall Duplicate Rate**: `22.26%`
  - **Overall Abandonment Rate**: `15.95%`
  - **Recovery Time**: **`p50 = 1.521s`**, **`p90 = 2.033s`**

#### Breakdown by Arm & Failure Mode (Neon Postgres)

| Variant | Failure Mode | Exposed | Primary (1 copy) | Duplicates (2+) | Abandoned | Primary Rate | Duplicate Rate |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `check_status` | `reply_lost` | 76 | 67 | 0 | 9 | 88.2% | 0.0% |
| `check_status` | `request_lost` | 75 | 56 | 0 | 19 | 74.7% | 0.0% |
| `retry` | `reply_lost` | 75 | 0 | 67 | 8 | 0.0% | 89.3% |
| `retry` | `request_lost` | 75 | 63 | 0 | 12 | 84.0% | 0.0% |

### Transparent Disclosure (Simulated + Live Traffic)

- Track 01 explicitly permits simulated traffic when real human volume is limited.
- Alongside manual live session verification (`1` pre-existing `live` session and `1` `practice` session), **`300` end-to-end HTTP sessions** (each with an isolated `httpx.Client` cookie jar) were driven through the live deployed **Vercel -> Render -> Neon Postgres -> Flagsmith Cloud** stack using [`api/scripts/simulate_traffic.py`](api/scripts/simulate_traffic.py) (`--execute --seed 42 --sessions 300`).
- Modeled behavioral probabilities (deterministic per-visitor RNG from `--seed 42`):
  - **`control`**: `88%` click retry + display receipt; `12%` abandon without retrying.
  - **`check-status-first`**: `90%` check submission status (`10%` abandon immediately); when `found: true`, `95%` display receipt (`5%` close tab early); when `found: false`, `82%` proceed to retry + display receipt (`18%` abandon due to the extra step).

---

## Local Setup & Verification

### Environment Variables (`.env.example`)

Copy `.env.example` to `.env` in the repository root and configure:

| Variable | Default / Example | Description |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/one_copy` | PostgreSQL connection URL (Neon / Render / local Docker) |
| `FLAGSMITH_ENVIRONMENT_KEY` | *(empty)* | Flagsmith server-side key (`ser.*` required for local evaluation) |
| `FLAGSMITH_API_URL` | *(empty)* | Optional Flagsmith API base URL override |
| `FLAGSMITH_EVENTS_API_URL` | *(empty)* | Optional Flagsmith events API URL override |
| `EXPERIMENT_NAME` | `submission_recovery` | Flagsmith experiment & feature key |
| `RECOVERY_WINDOW_SECONDS` | `60` | Recovery countdown window in seconds |
| `APP_COHORT` | `qa` | Enrolled cohort label (`qa` in Development, `live` in Production) |
| `COOKIE_SECRET` | `change-me-dev-cookie-secret-32-bytes-minimum` | HMAC secret for signing `oc_visitor` cookies |
| `BALANCE_SECRET` | `change-me-dev-balance-secret-32-bytes-minimum` | HMAC secret for paired failure mode balancing |
| `APP_ORIGIN` | `http://localhost:3000` | Comma-separated allowed browser origins for CSRF `Origin` checks |
| `ENVIRONMENT` | `development` | Runtime mode: `development`, `qa`, or `production` |

### Backend (`api/`)

#### Windows PowerShell
```powershell
python -m venv api\.venv
.\api\.venv\Scripts\Activate.ps1
pip install -r api\requirements.lock
uvicorn app.main:app --app-dir api --reload --port 8000
```

#### macOS / Linux
```bash
python3 -m venv api/.venv
source api/.venv/bin/activate
pip install -r api/requirements.lock
uvicorn app.main:app --app-dir api --reload --port 8000
```

### Frontend (`web/`)

```bash
cd web
npm install
npm run dev
```

Vite proxies `/api/*` requests to `http://localhost:8000` during local development (`web/vite.config.ts`).

### Verification Commands

```powershell
# Backend unit tests (and optional live Postgres integration test)
.\api\.venv\Scripts\Activate.ps1
Push-Location api; pytest -v; Pop-Location

# With live Postgres URL:
$env:TEST_DATABASE_URL="<your-postgres-url>"; Push-Location api; pytest -v; Pop-Location

# Frontend TypeScript check + production Vite bundle
Push-Location web; npm run build; Pop-Location

# Behavioral traffic simulator (dry-run by default; add --execute to run live)
python api\scripts\simulate_traffic.py --seed 42 --sessions 300
```
