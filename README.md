# CloudWatch + Bedrock RCA PoC

A proof-of-concept observability pipeline for Kubernetes pods on EC2. A faulty workload intentionally emits errors and latency spikes, telemetry flows into CloudWatch via the ADOT Collector, and an analyzer service uses Amazon Bedrock (Claude 3 Haiku) to generate a concise root-cause analysis on demand.

```
faulty-workload  →  ADOT Collector  →  CloudWatch Logs / Metrics / X-Ray
                                              ↓
                                         analyzer  →  Bedrock (Claude)  →  RCA JSON
```

## Local Docker (quickstart — no Kubernetes needed)

You can run both services on your laptop with `docker compose`. The only AWS calls that happen are CloudWatch (log/metric writes from the workload, reads from the analyzer) and Bedrock (the RCA invocation) — your local `~/.aws` credentials are mounted read-only into the containers.

**Prerequisites:** Docker, `curl`, `jq`, AWS credentials with CloudWatch + Bedrock access.

```bash
# 1. Set your region (defaults to us-east-1 if omitted)
export AWS_REGION=us-east-1

# 2. Build and start both services
docker compose up --build

# 3. Check they're healthy
curl http://localhost:8080/          # faulty-workload — expect {"status":"ok"}
curl http://localhost:8000/health    # analyzer        — expect {"status":"ok"}

# 4. Open the incident UI
open http://localhost:8000
```

To run the end-to-end smoke test against the local stack:

```bash
chmod +x scripts/smoke_test.sh
WORKLOAD_URL=http://localhost:8080 ANALYZER_URL=http://localhost:8000 ./scripts/smoke_test.sh
```

> **Note:** The smoke test waits 60 seconds for CloudWatch ingestion. If the analyzer returns a 404 ("No CloudWatch data found"), wait a little longer and narrow the time window in the UI to just the last few minutes.

To stop:

```bash
docker compose down
```

---

## Prerequisites (Kubernetes / production)

- Docker (for local builds)
- A Kubernetes cluster on EC2 (EKS recommended) with `kubectl` configured
- AWS CLI configured with access to the target account/region
- `curl` and `jq` (for the smoke test)
- Bedrock model access enabled: `anthropic.claude-3-haiku-20240307-v1:0`

## Repository layout

```
faulty-workload/   FastAPI service that generates controlled faults
analyzer/          FastAPI service that queries CloudWatch and calls Bedrock
k8s/               Kubernetes manifests (workload, analyzer, OTEL collector)
cloudwatch/        Dashboard and alarm definitions
scripts/           End-to-end smoke test
```

---

## 1. One-time AWS setup

### 1a. IAM roles (IRSA)

Two IAM roles are required. Attach them to the EKS OIDC provider for your cluster.

**OTEL Collector role** (`poc-otel-collector-role`) — needs:
- `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents`, `logs:DescribeLogGroups`, `logs:DescribeLogStreams`
- `cloudwatch:PutMetricData`
- `xray:PutTraceSegments`, `xray:PutTelemetryRecords`, `xray:GetSamplingRules`, `xray:GetSamplingTargets`

**Analyzer role** (`poc-analyzer-role`) — needs:
- `logs:StartQuery`, `logs:GetQueryResults`, `logs:StopQuery`
- `cloudwatch:GetMetricStatistics`
- `bedrock:InvokeModel` on `arn:aws:bedrock:*::foundation-model/anthropic.claude-3-haiku-20240307-v1:0`

### 1b. Patch the manifests with your account details

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=$(aws configure get region)
CLUSTER_OIDC=$(aws eks describe-cluster --name <your-cluster> --query "cluster.identity.oidc.issuer" --output text | sed 's|https://||')

# Analyzer IRSA annotation
sed -i "s/ACCOUNT_ID/${ACCOUNT_ID}/g" k8s/analyzer.yaml

# OTEL collector IRSA annotation
OTEL_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/poc-otel-collector-role"
sed -i "s|\${OTEL_COLLECTOR_ROLE_ARN}|${OTEL_ROLE_ARN}|g" k8s/otel-collector.yaml
```

---

## 2. Build and push Docker images

Replace `<ECR_REGISTRY>` with your ECR registry URI (e.g. `123456789012.dkr.ecr.us-east-1.amazonaws.com`).

```bash
aws ecr get-login-password --region $REGION | \
  docker login --username AWS --password-stdin <ECR_REGISTRY>

# faulty-workload
docker build -t faulty-workload:latest ./faulty-workload
docker tag faulty-workload:latest <ECR_REGISTRY>/faulty-workload:latest
docker push <ECR_REGISTRY>/faulty-workload:latest

# analyzer
docker build -t analyzer:latest ./analyzer
docker tag analyzer:latest <ECR_REGISTRY>/analyzer:latest
docker push <ECR_REGISTRY>/analyzer:latest
```

Update the `image:` fields in `k8s/faulty-workload.yaml` and `k8s/analyzer.yaml` to the full ECR URIs before deploying.

---

## 3. Deploy to Kubernetes

Apply the manifests in order:

```bash
# 1. OTEL Collector (DaemonSet — ships telemetry to CloudWatch)
kubectl apply -f k8s/otel-collector.yaml

# 2. Faulty workload
kubectl apply -f k8s/faulty-workload.yaml

# 3. Analyzer
kubectl apply -f k8s/analyzer.yaml

# Verify everything is running
kubectl get pods
kubectl get svc
```

Expected output once healthy:

```
NAME                            READY   STATUS    RESTARTS
otel-collector-xxxxx            1/1     Running   0
faulty-workload-xxxxx           1/1     Running   0
analyzer-xxxxx                  1/1     Running   0
```

---

## 4. Set up CloudWatch dashboard and alarms

```bash
REGION=$(aws configure get region)

# Create the dashboard
aws cloudwatch put-dashboard \
  --dashboard-name POC-FaultyWorkload \
  --dashboard-body file://cloudwatch/dashboard.json \
  --region $REGION

# Create the alarms (one call per alarm definition)
for alarm in $(jq -c '.[]' cloudwatch/alarms.json); do
  aws cloudwatch put-metric-alarm \
    --cli-input-json "$alarm" \
    --region $REGION
done
```

---

## 5. Run the smoke test

Port-forward both services (or use the cluster DNS if running from within the cluster):

```bash
kubectl port-forward svc/faulty-workload 8080:8080 &
kubectl port-forward svc/analyzer 8000:8000 &
```

Then run the end-to-end test:

```bash
chmod +x scripts/smoke_test.sh
./scripts/smoke_test.sh
```

The script sends 50 requests to the faulty workload, waits 60 seconds for CloudWatch ingestion, calls `POST /rca/analyze`, and asserts the response contains all five RCA fields with `confidence > 0`.

To target different endpoints:

```bash
WORKLOAD_URL=http://my-workload-host:8080 \
ANALYZER_URL=http://my-analyzer-host:8000 \
./scripts/smoke_test.sh
```

---

## 6. Open the UI

```bash
kubectl port-forward svc/analyzer 8000:8000
```

Then open [http://localhost:8000](http://localhost:8000). Fill in an incident ID, service name (`faulty-workload`), and a time window that overlaps with when you ran the smoke test, then click **Analyze Incident**.

---

## Local development (no Kubernetes)

You can run both services locally against real AWS credentials:

```bash
# Terminal 1 — faulty workload
cd faulty-workload
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8080 --reload

# Terminal 2 — analyzer
cd analyzer
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

AWS credentials are picked up from the environment or `~/.aws`. Make sure the credentials have the same permissions described in section 1a.

---

## Fault injection tuning

The faulty workload behaviour is controlled by environment variables in `k8s/faulty-workload.yaml`:

| Variable | Default | Description |
|---|---|---|
| `FAULT_RATE` | `0.05` | Probability of a random HTTP 500 (5%) |
| `LATENCY_SPIKE_INTERVAL` | `20` | Inject a 2–5s latency spike every N requests |
| `TIMEOUT_INTERVAL` | `50` | Simulate a dependency timeout every N requests |
| `FAULT_SAMPLE_RATE` | `0.1` | Fraction of fault-free requests that emit a log line (reduces CloudWatch cost) |

---

## Cost notes

- CloudWatch log retention is set to **3 days** in the OTEL collector config.
- Only ERROR and WARNING log lines are queried by the analyzer (top 20 per incident).
- Bedrock prompts target under 500 tokens; a warning is logged if this is exceeded.
- For a light POC workload the total AWS cost should remain well within free-tier or single-digit USD per day.
