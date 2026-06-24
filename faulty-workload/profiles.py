"""Fault profile registry.

Maps WORKLOAD_FAMILY env-var values to per-variant fault parameter overrides.
"""

from __future__ import annotations

PROFILES: dict[str, dict[str, str]] = {
    "default": {
        "HTTP_500_PROBABILITY": "0.05",
        "LATENCY_SPIKE_PROBABILITY": "0.05",
        "LATENCY_SPIKE_MIN_S": "2.0",
        "LATENCY_SPIKE_MAX_S": "5.0",
        "TIMEOUT_EVERY_N": "50",
        "MEMORY_PRESSURE_THRESHOLD": "100",
    },
    "customers": {
        "HTTP_500_PROBABILITY": "0.03",
        "LATENCY_SPIKE_PROBABILITY": "0.08",
        "LATENCY_SPIKE_MIN_S": "1.0",
        "LATENCY_SPIKE_MAX_S": "3.0",
        "TIMEOUT_EVERY_N": "80",
        "MEMORY_PRESSURE_THRESHOLD": "60",
    },
    "orders": {
        "HTTP_500_PROBABILITY": "0.04",
        "LATENCY_SPIKE_PROBABILITY": "0.06",
        "LATENCY_SPIKE_MIN_S": "3.0",
        "LATENCY_SPIKE_MAX_S": "8.0",
        "TIMEOUT_EVERY_N": "30",
        "MEMORY_PRESSURE_THRESHOLD": "120",
    },
    "auth": {
        "HTTP_500_PROBABILITY": "0.08",
        "LATENCY_SPIKE_PROBABILITY": "0.10",
        "LATENCY_SPIKE_MIN_S": "0.5",
        "LATENCY_SPIKE_MAX_S": "2.0",
        "TIMEOUT_EVERY_N": "100",
        "MEMORY_PRESSURE_THRESHOLD": "200",
    },
}


def get_profile(family: str) -> dict[str, str]:
    """Return fault parameters for a workload family.

    Falls back to 'default' if the family is not registered.
    """
    return PROFILES.get(family, PROFILES["default"])
