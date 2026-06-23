# Datadog + Bedrock RCA PoC

A proof-of-concept observability pipeline for Kubernetes pods on EC2. A faulty workload intentionally emits errors and latency spikes, telemetry flows into Datadog via the Datadog Agent, and an analyzer service uses Amazon Bedrock (Claude 3 Haiku) plus the Datadog MCP server to generate a concise root-cause analysis on demand.

```
faulty-workload  →  Datadog Agent  →  Datadog Backend (Logs / Metrics / APM / LLM Obs)
                                              ↓
                                   Datadog MCP Server  →  Datadog API
                                              ↓
                                         analyzer  →  Bedrock (Claude)  →  RCA JSON
```

---

## Prerequisites

- **Docker** and Docker Compose
- **AWS credentials** with Bedrock access (`bedrock:InvokeModel` on Claude 3 Haiku)
- **Datadog account** with:
  - `DD_API_KEY` — Datadog API key (used by the Agent and LLM Observability)
  - `DD_APP_KEY` — Datadog Application key (used by the MCP server for queries)
- `curl` and `jq` (for the smoke test)

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Docker Compose (local) / EKS (production)              │
│                                                         │
│  faulty-workload :8080                                  │
│    ├─ stdout JSON logs ──┐                              │
│    ├─ /metrics (Prom) ───┤                              │
│    └─ ddtrace APM ───────┤                              │
│                           ▼                             │
│              Datadog Agent :8126                         │
│                    │                                    │
│                    │ HTTPS                              │
│                    ▼                                    │
│           Datadog Backend                               │
│       (Logs · Metrics · APM · LLM Obs)                  │
│                    ▲                                    │
│                    │ Datadog API                        │
│                    │                                    │
│         Datadog MCP Server :3000                        │
│                    ▲                                    │
│                    │ MCP tool calls                     │
│                    │                                    │
│           analyzer :8000                                │
│              │                                          │
│              └──── Bedrock (Claude 3 Haiku) ────► RCA   │
└─────────────────────────────────────────────────────────┘
```

### Data Flow

1. `faulty-workload` emits structured JSON logs, Prometheus metrics, and APM traces.
2. The **Datadog Agent** collects all signals and forwards them to the Datadog backend.
3. On `POST /rca/analyze`, the **analyzer** retrieves incident evidence via **Datadog MCP** tool calls.
4. Evidence is compressed into an incident bundle and sent to **AWS Bedrock** (Claude 3 Haiku).
5. Bedrock returns a structured RCA response with triage, root cause, evidence summary, recommended focus, and confidence.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `DD_API_KEY` | Yes | Datadog API key (Agent + LLM Observability) |
| `DD_APP_KEY` | Yes | Datadog Application key (MCP server queries) |
| `DD_SITE` | No | Datadog site (default: `datadoghq.com`) |
| `AWS_REGION` | No | AWS region for Bedrock (default: `us-east-1`) |
| `AWS_ACCESS_KEY_ID` | Yes | AWS access key with Bedrock permissions |
| `AWS_SECRET_ACCESS_KEY` | Yes | AWS secret key |
| `AWS_SESSION_TOKEN` | No | AWS session token (if using temporary credentials) |
| `BEDROCK_MODEL_ID` | No | Bedrock model (default: `anthropic.claude-3-haiku-20240307-v1:0`) |
| `BEDROCK_MAX_TOKENS` | No | Max tokens for Bedrock response (default: `512`) |

See `.env.example` for the full list including fault-tuning and smoke-test variables.

---

## Local Quickstart (Docker Compose)

```bash
# 1. Create your .env file from the example
cp .env.example .env

# 2. Fill in your Datadog and AWS credentials
#    Required: DD_API_KEY, DD_APP_KEY, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
$EDITOR .env

# 3. Build and start all services
docker compose up --build

# 4. Verify services are healthy
curl http://localhost:8080/          # faulty-workload → {"status":"ok"}
curl http://localhost:8000/health    # analyzer        → {"status":"ok"}

# 5. Wait ~90 seconds for Datadog ingestion, then run the smoke test
./scripts/smoke_test.sh

# 6. Open the RCA UI
open http://localhost:8000
```

The smoke test sends 50 requests to the faulty workload, waits 90 seconds for Datadog ingestion, calls `POST /rca/analyze`, and asserts the response contains all five RCA fields (`triage_result`, `root_cause`, `evidence`, `recommended_focus`, `confidence`).

To stop:

```bash
docker compose down
```

---

## Repository Layout

```
faulty-workload/   FastAPI service that generates controlled faults
analyzer/          FastAPI service that queries Datadog MCP and calls Bedrock
k8s/               Kubernetes manifests (workload, analyzer, Datadog Agent, MCP)
scripts/           End-to-end smoke test
```

---

## Kubernetes / EKS Deployment

### IAM (IRSA)

One IAM role is required for the analyzer. Attach it to the EKS OIDC provider.

**Analyzer role** (`poc-analyzer-role`) — needs:
- `bedrock:InvokeModel` on `arn:aws:bedrock:*::foundation-model/anthropic.claude-3-haiku-20240307-v1:0`

The Datadog Agent authenticates to the Datadog backend using `DD_API_KEY` (stored as a Kubernetes Secret), not AWS IAM.

### Deploy

```bash
# 1. Create the Datadog secret
kubectl create secret generic datadog-secret \
  --from-literal=api-key=$DD_API_KEY \
  --from-literal=app-key=$DD_APP_KEY

# 2. Deploy Datadog Agent (DaemonSet)
kubectl apply -f k8s/datadog-agent.yaml

# 3. Deploy Datadog MCP server
kubectl apply -f k8s/datadog-mcp.yaml

# 4. Deploy faulty workload
kubectl apply -f k8s/faulty-workload.yaml

# 5. Deploy analyzer
kubectl apply -f k8s/analyzer.yaml

# Verify
kubectl get pods
```

---

## Smoke Test

```bash
# Against local Docker Compose (default)
./scripts/smoke_test.sh

# Against custom endpoints
WORKLOAD_URL=http://my-workload:8080 \
ANALYZER_URL=http://my-analyzer:8000 \
./scripts/smoke_test.sh
```

---

## Fault Injection Tuning

The faulty workload behaviour is controlled by environment variables:

| Variable | Default | Description |
|---|---|---|
| `FAULT_SAMPLE_RATE` | `1.0` | Fraction of requests that emit a log line |
| `HTTP_500_PROBABILITY` | `0.05` | Probability of a random HTTP 500 (5%) |
| `LATENCY_SPIKE_PROBABILITY` | `0.05` | Probability of a 2–5s latency spike |
| `TIMEOUT_EVERY_N` | `50` | Simulate a dependency timeout every N requests |

---

## Local Development (no Docker)

Run services directly with `uvicorn` for the fastest iteration cycle:

```bash
# Terminal 1 — faulty-workload
cd faulty-workload
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8080 --reload

# Terminal 2 — Datadog MCP server
npx -y @datadog/mcp --transport http --port 3000
# Requires DD_API_KEY and DD_APP_KEY in environment

# Terminal 3 — analyzer
cd analyzer
pip install -r requirements.txt
DD_MCP_TRANSPORT=http DD_MCP_URL=http://localhost:3000 \
  uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

---

## Cost Notes

- Datadog log volume is controlled by `FAULT_SAMPLE_RATE` (set to `1.0` locally, lower in production).
- Only ERROR and WARNING log lines are queried by the analyzer (top 20 per incident).
- Bedrock prompts target under 500 tokens; a warning is logged if this is exceeded.
- For a light POC workload the total AWS cost should remain well within free-tier or single-digit USD per day.
