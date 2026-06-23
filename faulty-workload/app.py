"""faulty-workload FastAPI service."""

import os
import random
import time
import uuid

# ---------------------------------------------------------------------------
# Cost / volume tuning
# ---------------------------------------------------------------------------

# Fraction of fault-free requests that emit a "Health check OK" log line.
# Defaults to 0.1 (10 %) to reduce CloudWatch log ingestion on healthy traffic.
# Set to 1.0 to log every request; 0.0 to suppress healthy-request logs entirely.
_FAULT_SAMPLE_RATE: float = max(0.0, min(1.0, float(os.environ.get("FAULT_SAMPLE_RATE", "0.1"))))

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from context import request_id_var, trace_id_var
from faults import FaultResult, apply_faults
from logger import logger
from metrics import (
    error_count,
    latency_ms,
    metrics_app,
    request_count,
    timeout_count,
    warning_count,
)

app = FastAPI(
    title="faulty-workload",
    description="Intentionally faulty workload for CloudWatch / Bedrock RCA PoC",
    version="0.1.0",
)

# Mount the Prometheus metrics endpoint at /metrics via the prometheus-client
# ASGI app.  This is done with app.mount so that all other routes remain on
# the main FastAPI app.
app.mount("/metrics", metrics_app)


# ---------------------------------------------------------------------------
# Trace / Request ID middleware
# ---------------------------------------------------------------------------


class TraceContextMiddleware(BaseHTTPMiddleware):
    """Populate request-scoped context vars on every inbound request.

    Reads ``X-Trace-ID`` and ``X-Request-ID`` headers.  If a header is absent
    or empty, a new UUID4 is generated.  Both values are stored in
    :mod:`context` ``ContextVar``s so all downstream code (logger, metrics,
    fault handlers) can read them without explicit parameter passing.

    The resolved IDs are also echoed back in the response headers so callers
    can correlate their requests.
    """

    async def dispatch(self, request: Request, call_next):
        trace_id = request.headers.get("X-Trace-ID") or str(uuid.uuid4())
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())

        trace_id_var.set(trace_id)
        request_id_var.set(request_id)

        response = await call_next(request)

        # Echo IDs back so clients and load-balancers can correlate responses.
        response.headers["X-Trace-ID"] = trace_id
        response.headers["X-Request-ID"] = request_id

        return response


app.add_middleware(TraceContextMiddleware)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", summary="Health check")
async def health_check(request: Request) -> JSONResponse:
    """Return a liveness response, with fault injection applied.

    ``apply_faults`` may raise an ``HTTPException`` directly (e.g. random
    HTTP 500) or return a list of :class:`~faults.FaultResult` warning objects
    that are included in the response body for observability.

    ``trace_id`` and ``request_id`` are already stored in the request-scoped
    context vars by :class:`TraceContextMiddleware` and are picked up
    automatically by the JSON logger — no need to pass them explicitly.
    """
    request_count.inc()
    start_time = time.monotonic()

    try:
        # apply_faults raises HTTPException for hard faults; returns warnings otherwise.
        fault_warnings: list[FaultResult] = await apply_faults(request)
    except HTTPException as exc:
        error_count.inc()
        elapsed_ms = (time.monotonic() - start_time) * 1000
        latency_ms.observe(elapsed_ms)
        logger.error(
            "Hard fault triggered: %s",
            exc.detail,
            extra={"error_type": "http_exception"},
        )
        raise

    elapsed_ms = (time.monotonic() - start_time) * 1000
    latency_ms.observe(elapsed_ms)

    # Log a WARNING for each soft fault / warning returned.
    for w in fault_warnings:
        warning_count.labels(warning_type=w.warning_type).inc()
        if w.warning_type == "dependency_timeout":
            timeout_count.inc()
        logger.warning(w.message, extra={"error_type": w.warning_type})

    warning_payload = [
        {"warning_type": w.warning_type, "message": w.message}
        for w in fault_warnings
    ]

    # Only log the healthy-request line for a sampled fraction of requests to
    # avoid flooding CloudWatch with high-volume "all-clear" entries.
    if random.random() < _FAULT_SAMPLE_RATE:
        logger.info("Health check OK")

    return JSONResponse(
        content={
            "status": "ok",
            "service": "faulty-workload",
            **({"warnings": warning_payload} if warning_payload else {}),
        }
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8080, reload=False)
