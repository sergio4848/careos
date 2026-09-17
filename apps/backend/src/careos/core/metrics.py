"""Prometheus metrics. Labels are low-cardinality and never contain identifiers or PII."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter(
    "careos_http_requests_total",
    "HTTP requests handled",
    ["method", "route", "status"],
)
HTTP_LATENCY = Histogram(
    "careos_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
GATEWAY_EVENTS = Counter(
    "careos_gateway_events_total",
    "Device events received by the gateway",
    ["adapter", "event_type", "result"],
)
INCIDENTS_CREATED = Counter(
    "careos_incidents_created_total",
    "Incidents created",
    ["trigger_type", "priority"],
)
ESCALATION_ACTIONS = Counter(
    "careos_escalation_actions_total",
    "Escalation actions executed by the worker",
    ["action_type", "result"],
)
PROVIDER_CALLS = Counter(
    "careos_provider_calls_total",
    "Calls made to external provider abstractions",
    ["provider_kind", "provider", "result"],
)
WEBSOCKET_CONNECTIONS = Gauge(
    "careos_websocket_connections",
    "Open operator WebSocket connections on this instance",
)
REALTIME_PUBLISH_FAILURES = Counter(
    "careos_realtime_publish_failures_total",
    "Realtime messages that could not be published to the broker",
)
