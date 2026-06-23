"""
CloudWatch Logs Insights query logic and metrics retrieval for fetching incident evidence.
"""

import time
from datetime import datetime
from typing import List, Dict, Optional

import boto3


def query_logs(
    log_group: str,
    window_start: datetime,
    window_end: datetime,
    pod_name: Optional[str] = None,
) -> List[Dict[str, str]]:
    """
    Query CloudWatch Logs Insights for ERROR and WARNING severity events
    within the specified time window.

    Args:
        log_group: CloudWatch log group name (e.g., '/poc/faulty-workload')
        window_start: Start of the incident time window
        window_end: End of the incident time window
        pod_name: Optional pod name filter

    Returns:
        List of dicts with keys: timestamp, severity, error_type, message, trace_id
        Limited to top 20 results to control cost.
    """
    client = boto3.client("logs")

    # Build the CloudWatch Logs Insights query
    # Filter by severity ERROR or WARNING, optionally by pod_name
    query_parts = [
        "fields @timestamp, severity, error_type, message, trace_id",
        "| filter severity in ['ERROR', 'WARNING']",
    ]

    if pod_name:
        query_parts.append(f"| filter pod_name = '{pod_name}'")

    query_parts.extend([
        "| sort @timestamp desc",
        "| limit 20",
    ])

    query_string = "\n".join(query_parts)

    # Convert datetime to Unix epoch timestamps (seconds)
    start_time = int(window_start.timestamp())
    end_time = int(window_end.timestamp())

    # Start the query
    response = client.start_query(
        logGroupName=log_group,
        startTime=start_time,
        endTime=end_time,
        queryString=query_string,
    )

    query_id = response["queryId"]

    # Poll until the query completes
    max_attempts = 30
    attempt = 0
    while attempt < max_attempts:
        attempt += 1
        time.sleep(1)  # Wait 1 second between polls

        result = client.get_query_results(queryId=query_id)
        status = result["status"]

        if status == "Complete":
            # Parse the results into our desired format
            logs = []
            for result_row in result.get("results", []):
                # Each result_row is a list of dicts with 'field' and 'value' keys
                log_entry = {}
                for field_dict in result_row:
                    field_name = field_dict["field"]
                    field_value = field_dict["value"]
                    
                    # Map @timestamp to timestamp
                    if field_name == "@timestamp":
                        log_entry["timestamp"] = field_value
                    else:
                        log_entry[field_name] = field_value

                # Ensure all required fields exist (use empty string as fallback)
                logs.append({
                    "timestamp": log_entry.get("timestamp", ""),
                    "severity": log_entry.get("severity", ""),
                    "error_type": log_entry.get("error_type", ""),
                    "message": log_entry.get("message", ""),
                    "trace_id": log_entry.get("trace_id", ""),
                })

            return logs

        elif status == "Failed" or status == "Cancelled":
            raise RuntimeError(f"CloudWatch Logs Insights query {status.lower()}: {query_id}")

        # Otherwise status is "Running" or "Scheduled", continue polling

    # If we exhausted attempts
    raise TimeoutError(f"CloudWatch Logs Insights query timed out after {max_attempts} seconds")


def query_metrics(
    namespace: str,
    service: str,
    window_start: datetime,
    window_end: datetime,
) -> Dict[str, float]:
    """
    Query CloudWatch Metrics for key signals over the incident window and return
    the delta (max - min) for each metric.

    Metrics fetched:
      - request_count_total  (Sum statistics, 60s period)
      - error_count_total    (Sum statistics, 60s period)
      - warning_count_total  (Sum statistics, 60s period)
      - timeout_count_total  (Sum statistics, 60s period)
      - latency_ms           (p50 / p99 extended statistics, 60s period)

    Args:
        namespace:    CloudWatch metric namespace (e.g. 'POC/FaultyWorkload')
        service:      Service name used as the 'service' dimension value
        window_start: Start of the incident time window
        window_end:   End of the incident time window

    Returns:
        Dict with keys:
          request_count, error_count, warning_count, timeout_count,
          latency_p50_ms, latency_p99_ms
        Each value is the delta (max datapoint value - min datapoint value)
        over the window, or 0.0 when no datapoints were returned.
    """
    client = boto3.client("cloudwatch")
    period = 60  # seconds

    dimensions = [{"Name": "service", "Value": service}]

    def _delta_sum(metric_name: str) -> float:
        """Fetch Sum datapoints and return max - min."""
        response = client.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric_name,
            Dimensions=dimensions,
            StartTime=window_start,
            EndTime=window_end,
            Period=period,
            Statistics=["Sum"],
        )
        values = [dp["Sum"] for dp in response.get("Datapoints", [])]
        if not values:
            return 0.0
        return max(values) - min(values)

    def _delta_extended(metric_name: str, stat_key: str) -> float:
        """Fetch extended statistic datapoints and return max - min."""
        response = client.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric_name,
            Dimensions=dimensions,
            StartTime=window_start,
            EndTime=window_end,
            Period=period,
            ExtendedStatistics=[stat_key],
        )
        values = [
            dp["ExtendedStatistics"][stat_key]
            for dp in response.get("Datapoints", [])
            if stat_key in dp.get("ExtendedStatistics", {})
        ]
        if not values:
            return 0.0
        return max(values) - min(values)

    return {
        "request_count":  _delta_sum("request_count_total"),
        "error_count":    _delta_sum("error_count_total"),
        "warning_count":  _delta_sum("warning_count_total"),
        "timeout_count":  _delta_sum("timeout_count_total"),
        "latency_p50_ms": _delta_extended("latency_ms", "p50"),
        "latency_p99_ms": _delta_extended("latency_ms", "p99"),
    }
