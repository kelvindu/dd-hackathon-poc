import json
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv

load_dotenv()  # loads .env from CWD or any parent directory; no-op if not found

from fastapi import FastAPI, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import bedrock
import bundle
import datadog_mcp
from datadog_mcp import MCPUnavailableError, MCPQueryError

app = FastAPI(title="RCA Analyzer", version="0.1.0")
templates = Jinja2Templates(directory="templates")

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
    triage_result: str          # noise | watch | needs-attention
    root_cause: str
    evidence: str
    recommended_focus: str      # replaces recommended_fix
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
    Fetch incident evidence via Datadog MCP, compress it into a compact bundle,
    invoke Bedrock for root-cause analysis, and return the structured RCA result.
    """
    # ------------------------------------------------------------------
    # 1. Fetch evidence via Datadog MCP
    # ------------------------------------------------------------------
    try:
        evidence = datadog_mcp.fetch_evidence(
            service=body.service,
            window_start=body.window_start,
            window_end=body.window_end,
            pod_name=body.pod_name,
        )
    except (MCPUnavailableError, MCPQueryError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # ------------------------------------------------------------------
    # 2. Guard: no evidence → 404
    # ------------------------------------------------------------------
    if not evidence.logs and not evidence.monitors and not evidence.incidents:
        raise HTTPException(
            status_code=404,
            detail="No Datadog evidence found for the specified window",
        )

    # ------------------------------------------------------------------
    # 3. Build the compact incident bundle
    # ------------------------------------------------------------------
    incident_bundle = bundle.build_bundle(evidence)

    # ------------------------------------------------------------------
    # 4. Invoke Bedrock for RCA
    # ------------------------------------------------------------------
    try:
        rca = bedrock.invoke_rca(incident_bundle)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail=f"Bedrock non-JSON: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # ------------------------------------------------------------------
    # 5. Return the structured result
    # ------------------------------------------------------------------
    return AnalyzeResponse(
        triage_result=rca["triage_result"],
        root_cause=rca["root_cause"],
        evidence=rca["evidence"],
        recommended_focus=rca["recommended_focus"],
        confidence=float(rca["confidence"]),
    )
