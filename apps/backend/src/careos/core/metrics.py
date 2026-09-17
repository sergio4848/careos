"""Prometheus metrics.

Labels are low-cardinality and never contain identifiers or personal data. Counters that
describe persisted outcomes (incidents created, takeovers, resolutions) are incremented only
after the transaction commits, so a rollback can never inflate them.
"""

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

# --- device gateway -------------------------------------------------------------------
GATEWAY_EVENTS_RECEIVED = Counter(
    "careos_gateway_events_received_total",
    "Authenticated device events that passed contract validation (including duplicates)",
    ["adapter", "event_type"],
)
SOS_RECEIVED = Counter(
    "careos_sos_received_total",
    "Authenticated, contract-valid SOS_BUTTON events (duplicates and later rejections included)",
    ["adapter"],
)
GATEWAY_DUPLICATE_EVENTS = Counter(
    "careos_gateway_duplicate_events_total",
    "Redelivered events answered from the idempotency ledger",
    ["adapter", "event_type"],
)
GATEWAY_REJECTIONS = Counter(
    "careos_gateway_rejections_total",
    "Device events rejected by the gateway",
    ["adapter", "reason"],
)

# --- incidents --------------------------------------------------------------------------
INCIDENTS_CREATED = Counter(
    "careos_incidents_created_total",
    "Incidents committed",
    ["trigger_type", "priority"],
)
INCIDENT_TAKEOVERS = Counter(
    "careos_incident_takeovers_total",
    "Operator takeover attempts by outcome",
    ["result"],
)
INCIDENT_RESOLUTIONS = Counter(
    "careos_incident_resolutions_total",
    "Incident resolutions committed",
    ["category"],
)

# --- escalation and providers -------------------------------------------------------------
ESCALATION_ACTIONS = Counter(
    "careos_escalation_actions_total",
    "Escalation actions executed by the worker",
    ["action_type", "result"],
)
ESCALATION_FAILURES = Counter(
    "careos_escalation_failures_total",
    "Failed escalation step attempts (category: timeout, provider_error, unexpected, exhausted)",
    ["action_type", "category"],
)
PROVIDER_CALLS = Counter(
    "careos_provider_calls_total",
    "Calls made to provider abstractions",
    ["provider_kind", "provider", "result"],
)
PROVIDER_FAILURES = Counter(
    "careos_provider_failures_total",
    "Provider calls that failed (category: timeout, provider_error, unexpected)",
    ["provider_kind", "provider", "category"],
)
AI_TIMEOUTS = Counter(
    "careos_ai_timeouts_total",
    "AI provider calls abandoned after the timeout",
    ["provider"],
)

# --- realtime -----------------------------------------------------------------------------
WEBSOCKET_CONNECTIONS = Gauge(
    "careos_websocket_connections",
    "Open operator WebSocket connections on this instance",
)
REALTIME_DELIVERY_FAILURES = Counter(
    "careos_realtime_delivery_failures_total",
    "Realtime notifications not delivered (stage: broker_publish, broker_unavailable, "
    "after_commit, socket_send)",
    ["stage"],
)
