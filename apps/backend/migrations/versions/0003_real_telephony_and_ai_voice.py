"""Real telephony and AI voice (Sprint 2).

Generated with Alembic autogenerate, then reviewed and extended by hand:

  * ``calls.provider_call_id`` is renamed (not dropped) to ``provider_call_sid``;
  * the ``call_status``, ``ai_purpose`` and ``incident_event_type`` VARCHAR CHECKs are
    recreated with the new lifecycle values (autogenerate does not diff CHECKs);
  * provider/webhook idempotency uniques (ADR-017): one call per (scheduled action,
    attempt), unique provider SID, unique single-use media token, deduplicated
    ``call_events`` per provider callback identity;
  * ``call_events`` is made append-only with the existing ``careos_reject_mutation``
    trigger (raw audio is never stored, ADR-018).

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-17 18:01:08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CALL_STATUSES_OLD = "'INITIATED', 'ANSWERED', 'NO_ANSWER', 'BUSY', 'FAILED'"
CALL_STATUSES_NEW = (
    "'QUEUED', 'INITIATED', 'RINGING', 'ANSWERED', 'IN_PROGRESS', 'COMPLETED', "
    "'NO_ANSWER', 'BUSY', 'FAILED', 'CANCELLED', 'TIMED_OUT'"
)
AI_PURPOSES_OLD = "'AUTOMATED_CHECK_IN'"
AI_PURPOSES_NEW = "'AUTOMATED_CHECK_IN', 'AUTOMATED_VOICE_CALL'"
INCIDENT_EVENT_TYPES_OLD = (
    "'SOS_RECEIVED', 'FALL_RECEIVED', 'DEVICE_FAULT_RECEIVED', 'ALARM_REPEATED', "
    "'INCIDENT_CREATED', 'VALIDATION_STARTED', 'VALIDATION_WARNING', 'INCIDENT_OPENED', "
    "'PRIORITY_CHANGED', 'ESCALATION_SCHEDULED', 'ESCALATION_STEP_SKIPPED', "
    "'ESCALATION_STEP_FAILED', 'ESCALATION_HALTED', 'AUTOMATED_CALL_STARTED', "
    "'AUTOMATED_CALL_NO_ANSWER', 'AUTOMATED_CALL_ANSWERED', 'AI_CALL_STARTED', "
    "'AI_CALL_COMPLETED', 'AI_CALL_FAILED', 'TRUSTED_CONTACT_CALLED', "
    "'TRUSTED_CONTACT_NO_ANSWER', 'TRUSTED_CONTACT_NOTIFIED', 'CONTACT_ACKNOWLEDGED', "
    "'CALL_FAILED', 'NOTIFICATION_FAILED', 'OPERATORS_ALERTED', 'OPERATOR_TAKEOVER', "
    "'INCIDENT_RESOLVED', 'INCIDENT_CLOSED'"
)
INCIDENT_EVENT_TYPES_NEW = (
    INCIDENT_EVENT_TYPES_OLD + ", 'AUTOMATED_CALL_CANCELLED', 'OPERATOR_ESCALATION_REQUESTED'"
)


def _swap_check(table: str, name: str, column: str, values: str) -> None:
    # op.f() marks names as final so the naming convention is not applied a second time.
    op.drop_constraint(op.f(name), table, type_="check")
    op.create_check_constraint(op.f(name), table, f"{column} IN ({values})")


def upgrade() -> None:
    # --- widened lifecycle vocabularies (VARCHAR + CHECK) ---------------------------------
    _swap_check("calls", "ck_calls_call_status", "status", CALL_STATUSES_NEW)
    _swap_check("ai_sessions", "ck_ai_sessions_ai_purpose", "purpose", AI_PURPOSES_NEW)
    _swap_check(
        "incident_events",
        "ck_incident_events_incident_event_type",
        "event_type",
        INCIDENT_EVENT_TYPES_NEW,
    )

    # --- calls: real-call fields ----------------------------------------------------------
    op.alter_column("calls", "provider_call_id", new_column_name="provider_call_sid")
    op.add_column(
        "calls",
        sa.Column("direction", sa.String(length=8), server_default="OUTBOUND", nullable=False),
    )
    op.add_column(
        "calls",
        sa.Column(
            "structured_response",
            sa.Enum(
                "CAN_RESPOND",
                "CANNOT_RESPOND",
                "REQUEST_OPERATOR",
                name="call_structured_response",
                native_enum=False,
                create_constraint=True,
                length=24,
            ),
            nullable=True,
        ),
    )
    op.add_column("calls", sa.Column("from_number_masked", sa.String(length=32), nullable=True))
    op.add_column("calls", sa.Column("to_number_masked", sa.String(length=32), nullable=True))
    op.add_column("calls", sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("calls", sa.Column("duration_seconds", sa.Integer(), nullable=True))
    op.add_column("calls", sa.Column("failure_category", sa.String(length=40), nullable=True))
    op.add_column("calls", sa.Column("failure_code", sa.String(length=64), nullable=True))
    op.add_column("calls", sa.Column("media_token_digest", sa.String(length=64), nullable=True))
    op.add_column(
        "calls", sa.Column("media_connected_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint("direction_valid", "calls", "direction IN ('OUTBOUND')")
    op.create_check_constraint(
        "duration_non_negative", "calls", "duration_seconds IS NULL OR duration_seconds >= 0"
    )
    op.create_unique_constraint("uq_calls_organisation_id_id", "calls", ["organisation_id", "id"])
    op.create_index(
        "uq_calls_scheduled_action_attempt",
        "calls",
        ["scheduled_action_id", "attempt"],
        unique=True,
        postgresql_where=sa.text("scheduled_action_id IS NOT NULL"),
    )
    op.create_index(
        "uq_calls_provider_call_sid",
        "calls",
        ["provider_call_sid"],
        unique=True,
        postgresql_where=sa.text("provider_call_sid IS NOT NULL"),
    )
    op.create_index(
        "uq_calls_media_token_digest",
        "calls",
        ["media_token_digest"],
        unique=True,
        postgresql_where=sa.text("media_token_digest IS NOT NULL"),
    )

    # --- ai_sessions: link to the call and the structured advisory ------------------------
    op.add_column("ai_sessions", sa.Column("call_id", sa.Uuid(), nullable=True))
    op.add_column("ai_sessions", sa.Column("contact_established", sa.Boolean(), nullable=True))
    op.add_column("ai_sessions", sa.Column("requested_human_help", sa.Boolean(), nullable=True))
    op.add_column(
        "ai_sessions",
        sa.Column(
            "urgency_signal",
            sa.Enum(
                "NONE",
                "ASSISTANCE_REQUESTED",
                "POTENTIAL_EMERGENCY",
                name="ai_urgency_signal",
                native_enum=False,
                create_constraint=True,
                length=24,
            ),
            nullable=True,
        ),
    )
    op.add_column("ai_sessions", sa.Column("language", sa.String(length=16), nullable=True))
    op.create_foreign_key(
        "fk_ai_sessions_call_same_org",
        "ai_sessions",
        "calls",
        ["organisation_id", "call_id"],
        ["organisation_id", "id"],
        ondelete="RESTRICT",
    )

    # --- call_events: append-only ledger ---------------------------------------------------
    op.create_table(
        "call_events",
        sa.Column("call_id", sa.Uuid(), nullable=False),
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column(
            "event_type",
            sa.Enum(
                "CALL_REQUESTED",
                "CALL_INITIATED",
                "CALL_RINGING",
                "CALL_ANSWERED",
                "MEDIA_STREAM_CONNECTED",
                "AI_SESSION_STARTED",
                "AI_SESSION_COMPLETED",
                "AI_SESSION_FAILED",
                "DTMF_RECEIVED",
                "USER_RESPONSE_RECEIVED",
                "CALL_ENDED",
                "CALL_FAILED",
                "CALL_NO_ANSWER",
                "CALL_BUSY",
                "CALL_CANCELLED",
                "CALL_TIMED_OUT",
                name="call_event_type",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("provider_event_key", sa.String(length=160), nullable=True),
        sa.Column(
            "data", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organisation_id", "call_id"],
            ["calls.organisation_id", "calls.id"],
            name="fk_call_events_call_same_org",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organisation_id", "incident_id"],
            ["incidents.organisation_id", "incidents.id"],
            name="fk_call_events_incident_same_org",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organisation_id"],
            ["organisations.id"],
            name=op.f("fk_call_events_organisation_id_organisations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_call_events")),
    )
    op.create_index(
        "ix_call_events_call_id_created_at", "call_events", ["call_id", "created_at"], unique=False
    )
    op.create_index(
        op.f("ix_call_events_organisation_id"), "call_events", ["organisation_id"], unique=False
    )
    op.create_index(
        "uq_call_events_provider_key",
        "call_events",
        ["call_id", "provider_event_key"],
        unique=True,
        postgresql_where=sa.text("provider_event_key IS NOT NULL"),
    )
    op.execute(
        "CREATE TRIGGER call_events_append_only BEFORE UPDATE OR DELETE ON call_events "
        "FOR EACH ROW EXECUTE FUNCTION careos_reject_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS call_events_append_only ON call_events")
    op.drop_index(
        "uq_call_events_provider_key",
        table_name="call_events",
        postgresql_where=sa.text("provider_event_key IS NOT NULL"),
    )
    op.drop_index(op.f("ix_call_events_organisation_id"), table_name="call_events")
    op.drop_index("ix_call_events_call_id_created_at", table_name="call_events")
    op.drop_table("call_events")
    op.drop_constraint("fk_ai_sessions_call_same_org", "ai_sessions", type_="foreignkey")
    op.drop_column("ai_sessions", "language")
    op.drop_constraint(op.f("ck_ai_sessions_ai_urgency_signal"), "ai_sessions", type_="check")
    op.drop_column("ai_sessions", "urgency_signal")
    op.drop_column("ai_sessions", "requested_human_help")
    op.drop_column("ai_sessions", "contact_established")
    op.drop_column("ai_sessions", "call_id")
    op.drop_index(
        "uq_calls_media_token_digest",
        table_name="calls",
        postgresql_where=sa.text("media_token_digest IS NOT NULL"),
    )
    op.drop_index(
        "uq_calls_provider_call_sid",
        table_name="calls",
        postgresql_where=sa.text("provider_call_sid IS NOT NULL"),
    )
    op.drop_index(
        "uq_calls_scheduled_action_attempt",
        table_name="calls",
        postgresql_where=sa.text("scheduled_action_id IS NOT NULL"),
    )
    op.drop_constraint("uq_calls_organisation_id_id", "calls", type_="unique")
    op.drop_constraint(op.f("ck_calls_duration_non_negative"), "calls", type_="check")
    op.drop_constraint(op.f("ck_calls_direction_valid"), "calls", type_="check")
    op.drop_column("calls", "media_connected_at")
    op.drop_column("calls", "media_token_digest")
    op.drop_column("calls", "failure_code")
    op.drop_column("calls", "failure_category")
    op.drop_column("calls", "duration_seconds")
    op.drop_column("calls", "answered_at")
    op.drop_column("calls", "to_number_masked")
    op.drop_column("calls", "from_number_masked")
    op.drop_constraint(op.f("ck_calls_call_structured_response"), "calls", type_="check")
    op.drop_column("calls", "structured_response")
    op.drop_column("calls", "direction")
    op.alter_column("calls", "provider_call_sid", new_column_name="provider_call_id")
    _swap_check(
        "incident_events",
        "ck_incident_events_incident_event_type",
        "event_type",
        INCIDENT_EVENT_TYPES_OLD,
    )
    _swap_check("ai_sessions", "ck_ai_sessions_ai_purpose", "purpose", AI_PURPOSES_OLD)
    _swap_check("calls", "ck_calls_call_status", "status", CALL_STATUSES_OLD)
