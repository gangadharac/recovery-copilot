# Architecture

Revenue Recovery Agent is an event-driven recovery application with two supported entry paths:

- A local batch runner at `run_batch.py`, with deterministic and autonomous-agent modes.
- A FastAPI application at `app.main`, which exposes system health and Razorpay webhook routes.

## Package layout

| Area | Responsibility |
| --- | --- |
| `app/agents/` | Diagnosis, strategy, recovery, verification, and registered recovery tools. |
| `app/db/` | SQLAlchemy database setup, models, recovery state transitions, and audit logging. |
| `app/executor/` | Recovery-action dispatch, message generation, and deterministic gateway simulation. |
| `app/generator/` | Synthetic transaction-data generation and loading. |
| `app/integrations/razorpay/` | Razorpay Test Mode client and payment-link adapter. |
| `app/pipeline/` | Batch processing orchestration. |
| `app/reporting/` | Streamlit dashboard. |
| `app/schemas/` | Pydantic contracts for transactions, diagnosis, strategy, audit, and agent state. |
| `app/webhooks/` | Razorpay webhook verification, payload processing, event correlation, and recovery triggering. |

## Runtime flow

```text
Batch input or Razorpay webhook
        |
        v
Normalize and persist transaction/event
        |
        v
Observe -> Diagnose -> Plan -> Guardrail check -> Execute -> Verify
        |                                                     |
        +------------------- Re-plan when needed ------------+
```

For Razorpay events, the FastAPI route first reads the raw request body and verifies the Razorpay HMAC signature. The webhook service then deduplicates and persists the event before handling the event type. Failed-payment events can be normalized for recovery; lifecycle events are correlated to existing records. Payment-link recovery remains pending until a later event passes the configured correlation, amount, and currency checks.

## Configuration and storage

`app/config.py` keeps the project-root `data/` paths centralized. The local SQLite database is `data/recovery_audit.db`; the synthetic benchmark datasets remain in `data/`. Configuration is read from a root `.env` file when one exists. `.env.example` is the tracked template, while `.env` is deliberately ignored.

The application guards against Razorpay live keys: it is intended for Test Mode credentials only. This document describes the existing implementation; it does not add integration behavior or operational guarantees.

## Developer entry points

```text
python run_batch.py
python run_batch.py --agent
uvicorn app.main:app --reload --port 8000
streamlit run app/reporting/dashboard.py
pytest -q
```
