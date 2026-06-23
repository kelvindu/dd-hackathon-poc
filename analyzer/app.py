import json
import os
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv

load_dotenv()  # loads .env from CWD or any parent directory; no-op if not found

from fastapi import FastAPI, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import bedrock
import bundle
import cloudwatch

app = FastAPI(title="RCA Analyzer", version="0.1.0")
templates = Jinja2Templates(directory="templates")

# ---------------------------------------------------------------------------
# Environment defaults (kept as fallback values only; .env is the source of truth)
# ---------------------------------------------------------------------------

_DEFAULT_LOG_GROUP = "/poc/faulty-workload"
_DEFAULT_METRIC_NAMESPACE = "POC/FaultyWorkload"


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class AnalyzeRequest(BaseModel):
    incident_id: str
    service: str
    window_start: datetime
    window_end: datetime
    namespace: Optional[str] = None
    pod_name: Optional[str] = None


class AnalyzeResponse(BaseModel):
    root_cause: str
    evidence: str
    impact: str
    recommended_fix: str
    confidence: float


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/rca/analyze", response_model=AnalyzeResponse)
def rca_analyze(body: AnalyzeRequest) -> AnalyzeResponse:
    """
    Query CloudWatch for logs and metrics over the incident window, compress
    the evidence into a compact bundle, invoke Bedrock for root-cause analysis,
    and return the structured RCA result.
    """
    log_group = os.environ.get("LOG_GROUP", _DEFAULT_LOG_GROUP)
    metric_namespace = os.environ.get("METRIC_NAMESPACE", _DEFAULT_METRIC_NAMESPACE)

    # ------------------------------------------------------------------
    # 1. Fetch CloudWatch data
    # ------------------------------------------------------------------
    try:
        logs = cloudwatch.query_logs(
            log_group=log_group,
            window_start=body.window_start,
            window_end=body.window_end,
            pod_name=body.pod_name,
        )
        metrics = cloudwatch.query_metrics(
            namespace=metric_namespace,
            service=body.service,
            window_start=body.window_start,
            window_end=body.window_end,
        )
    except (TimeoutError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # ------------------------------------------------------------------
    # 2. Guard: no usable data → 404
    # ------------------------------------------------------------------
    metrics_total = sum(metrics.values()) if metrics else 0.0
    if not logs and metrics_total == 0.0:
        raise HTTPException(
            status_code=404,
            detail="No CloudWatch data found for the specified window",
        )

    # ------------------------------------------------------------------
    # 3. Build the compact incident bundle
    # ------------------------------------------------------------------
    incident_bundle = bundle.build_bundle(logs=logs, metrics=metrics)

    # ------------------------------------------------------------------
    # 4. Invoke Bedrock for RCA
    # ------------------------------------------------------------------
    try:
        rca = bedrock.invoke_rca(incident_bundle)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Bedrock returned non-JSON output: {exc}",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # ------------------------------------------------------------------
    # 5. Return the structured result
    # ------------------------------------------------------------------
    return AnalyzeResponse(
        root_cause=rca["root_cause"],
        evidence=rca["evidence"],
        impact=rca["impact"],
        recommended_fix=rca["recommended_fix"],
        confidence=float(rca["confidence"]),
    )
