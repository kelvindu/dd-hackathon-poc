"""Fault injection module for the faulty-workload service.

Exposes a single ``apply_faults`` coroutine that the request handler calls
on every incoming request.  The module tracks a global request counter
(thread-safe) and uses it to trigger deterministic faults at configurable
intervals alongside probabilistic faults.

Environment variables
---------------------
TIMEOUT_EVERY_N : int, default 50
    Simulate a dependency timeout once every N requests.
MEMORY_PRESSURE_THRESHOLD : int, default 100
    Emit a memory-pressure warning once the request counter crosses this
    value (and on every subsequent multiple).
"""

from __future__ import annotations

import asyncio
import os
import random
import threading
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException

# ---------------------------------------------------------------------------
# Configuration (read once at import time)
# ---------------------------------------------------------------------------

_TIMEOUT_EVERY_N: int = int(os.environ.get("TIMEOUT_EVERY_N", "50"))
_MEMORY_PRESSURE_THRESHOLD: int = int(os.environ.get("MEMORY_PRESSURE_THRESHOLD", "100"))

# Probability of an immediate HTTP 500 response (5 %).
_HTTP_500_PROBABILITY: float = 0.05

# Introduce a latency spike roughly once every 20 requests (5 % chance per
# request on average mirrors the 1-in-20 cadence).
_LATENCY_SPIKE_PROBABILITY: float = 1 / 20

# Spike duration range in seconds.
_LATENCY_SPIKE_MIN_S: float = 2.0
_LATENCY_SPIKE_MAX_S: float = 5.0

# ---------------------------------------------------------------------------
# Request counter (shared across threads, protected by a lock)
# ---------------------------------------------------------------------------

_counter_lock = threading.Lock()
_request_counter: int = 0


def _increment_counter() -> int:
    """Increment the global request counter and return the new value."""
    global _request_counter
    with _counter_lock:
        _request_counter += 1
        return _request_counter


# ---------------------------------------------------------------------------
# FaultResult dataclass
# ---------------------------------------------------------------------------


@dataclass
class FaultResult:
    """Carries a non-fatal fault outcome that the handler should log or surface.

    Attributes
    ----------
    warning_type : str
        A short machine-readable label for the warning (e.g. ``"memory_pressure"``
        or ``"bad_payload"``).
    message : str
        A human-readable description of the warning.
    """

    warning_type: str
    message: str


# ---------------------------------------------------------------------------
# Individual fault functions
# ---------------------------------------------------------------------------


def _maybe_http_500() -> None:
    """Raise an HTTP 500 with 5 % probability.

    Raises
    ------
    HTTPException
        With status_code 500 when the random draw fires.
    """
    if random.random() < _HTTP_500_PROBABILITY:
        raise HTTPException(
            status_code=500,
            detail="Injected random internal server error",
        )


async def _maybe_latency_spike() -> None:
    """Sleep for 2–5 seconds with ~5 % probability (≈1 in 20 requests).

    Uses ``asyncio.sleep`` so the event loop is not blocked.
    """
    if random.random() < _LATENCY_SPIKE_PROBABILITY:
        delay = random.uniform(_LATENCY_SPIKE_MIN_S, _LATENCY_SPIKE_MAX_S)
        await asyncio.sleep(delay)


def _maybe_timeout(request_count: int) -> Optional[FaultResult]:
    """Simulate a dependency timeout every ``TIMEOUT_EVERY_N`` requests.

    Parameters
    ----------
    request_count : int
        The current (already-incremented) request counter value.

    Returns
    -------
    FaultResult or None
        A ``FaultResult`` with ``warning_type="dependency_timeout"`` when the
        condition fires, otherwise ``None``.
    """
    if _TIMEOUT_EVERY_N > 0 and request_count % _TIMEOUT_EVERY_N == 0:
        return FaultResult(
            warning_type="dependency_timeout",
            message=(
                f"Simulated dependency timeout on request #{request_count} "
                f"(fires every {_TIMEOUT_EVERY_N} requests)"
            ),
        )
    return None


def _maybe_memory_pressure(request_count: int) -> Optional[FaultResult]:
    """Emit a memory-pressure warning when the counter crosses the threshold.

    The warning fires on the threshold request and on every subsequent
    multiple of the threshold.

    Parameters
    ----------
    request_count : int
        The current (already-incremented) request counter value.

    Returns
    -------
    FaultResult or None
        A ``FaultResult`` with ``warning_type="memory_pressure"`` when the
        condition fires, otherwise ``None``.
    """
    if _MEMORY_PRESSURE_THRESHOLD > 0 and request_count % _MEMORY_PRESSURE_THRESHOLD == 0:
        return FaultResult(
            warning_type="memory_pressure",
            message=(
                f"Memory pressure warning: request counter crossed "
                f"{request_count} (threshold={_MEMORY_PRESSURE_THRESHOLD})"
            ),
        )
    return None


def _maybe_bad_payload(request) -> Optional[FaultResult]:
    """Check for missing or malformed fields in the incoming request.

    Inspects query parameters and (for non-GET methods) the JSON body when
    available.  Returns a warning if required fields are absent.

    Parameters
    ----------
    request : fastapi.Request
        The incoming FastAPI request object.

    Returns
    -------
    FaultResult or None
        A ``FaultResult`` with ``warning_type="bad_payload"`` when a problem
        is detected, otherwise ``None``.
    """
    # For demonstration, flag requests that supply a ``payload`` query param
    # whose value is the literal string "bad" or "malformed".
    payload_param: str = request.query_params.get("payload", "")
    if payload_param in ("bad", "malformed"):
        return FaultResult(
            warning_type="bad_payload",
            message=(
                f"Received malformed/missing request field: "
                f"payload={payload_param!r}"
            ),
        )
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def apply_faults(request) -> list[FaultResult]:
    """Apply all configured fault-injection rules to an incoming request.

    Call this at the top of every request handler.  The function may raise
    an ``HTTPException`` directly (for error-class faults) or return a list
    of :class:`FaultResult` objects that the caller should log or include in
    the response (for warning-class faults).

    Parameters
    ----------
    request : fastapi.Request
        The incoming FastAPI request object.

    Returns
    -------
    list[FaultResult]
        Zero or more non-fatal fault results that the handler should surface.

    Raises
    ------
    HTTPException
        When a hard fault fires (e.g. random 500, or re-raised by the caller
        for a timeout simulation treated as fatal).
    """
    request_count = _increment_counter()

    # --- Hard faults (raise immediately) ------------------------------------
    _maybe_http_500()

    # --- Soft async faults (await, then continue) ---------------------------
    await _maybe_latency_spike()

    # --- Warning-class faults (collect and return) --------------------------
    warnings: list[FaultResult] = []

    timeout_result = _maybe_timeout(request_count)
    if timeout_result:
        warnings.append(timeout_result)

    memory_result = _maybe_memory_pressure(request_count)
    if memory_result:
        warnings.append(memory_result)

    bad_payload_result = _maybe_bad_payload(request)
    if bad_payload_result:
        warnings.append(bad_payload_result)

    return warnings
