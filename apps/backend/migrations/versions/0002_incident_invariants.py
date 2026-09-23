"""Database-enforced incident invariants (ADR-013).

Hand-written. Moves safety rules that were only enforced in Python into PostgreSQL, so a bug,
a script or a future service cannot bypass them:

  * tenant-safe composite foreign keys for every remaining single-column reference on
    incidents, incident events, device event receipts and calls;
  * one incident per device event receipt;
  * CHECK constraints: human-only outcomes, AI never changes status, resolution/closure fields,
    positive timeline sequences, assignment release order, receipt outcome linkage, running
    scheduled actions hold a lease;
  * ``incident_status_transitions``: a snapshot of the state machine (drift-tested);
  * deferred constraint triggers, checked at COMMIT: the timeline's status events form a legal
    chain starting at RECEIVED, and ``incidents.status`` equals the last recorded status.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-17 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Snapshot of careos.modules.incident_engine.state_machine.TRANSITIONS at this revision.
# tests/integration/test_safety_invariants.py fails if the table and the code ever differ.
TRANSITIONS: tuple[tuple[str, str], ...] = (
    ("RECEIVED", "DEVICE_ERROR"),
    ("RECEIVED", "FAILED"),
    ("RECEIVED", "VALIDATING"),
    ("VALIDATING", "CANCELLED"),
    ("VALIDATING", "DEVICE_ERROR"),
    ("VALIDATING", "FAILED"),
    ("VALIDATING", "OPEN"),
    ("OPEN", "ACKNOWLEDGED"),
    ("OPEN", "CANCELLED"),
    ("OPEN", "CONTACTING"),
    ("OPEN", "ESCALATED"),
    ("OPEN", "FAILED"),
    ("OPEN", "FALSE_ALARM"),
    ("OPEN", "IN_PROGRESS"),
    ("OPEN", "RESOLVED"),
    ("CONTACTING", "ACKNOWLEDGED"),
    ("CONTACTING", "CANCELLED"),
    ("CONTACTING", "ESCALATED"),
    ("CONTACTING", "FAILED"),
    ("CONTACTING", "FALSE_ALARM"),
    ("CONTACTING", "IN_PROGRESS"),
    ("CONTACTING", "RESOLVED"),
    ("ESCALATED", "ACKNOWLEDGED"),
    ("ESCALATED", "CANCELLED"),
    ("ESCALATED", "CONTACTING"),
    ("ESCALATED", "FALSE_ALARM"),
    ("ESCALATED", "IN_PROGRESS"),
    ("ESCALATED", "RESOLVED"),
    ("ACKNOWLEDGED", "ESCALATED"),
    ("ACKNOWLEDGED", "FALSE_ALARM"),
    ("ACKNOWLEDGED", "IN_PROGRESS"),
    ("ACKNOWLEDGED", "RESOLVED"),
    ("IN_PROGRESS", "ESCALATED"),
    ("IN_PROGRESS", "FALSE_ALARM"),
    ("IN_PROGRESS", "RESOLVED"),
    ("FAILED", "ESCALATED"),
    ("FAILED", "FALSE_ALARM"),
    ("FAILED", "IN_PROGRESS"),
    ("FAILED", "RESOLVED"),
    ("DEVICE_ERROR", "ESCALATED"),
    ("DEVICE_ERROR", "FALSE_ALARM"),
    ("DEVICE_ERROR", "IN_PROGRESS"),
    ("DEVICE_ERROR", "RESOLVED"),
    ("RESOLVED", "CLOSED"),
    ("RESOLVED", "IN_PROGRESS"),
    ("FALSE_ALARM", "CLOSED"),
    ("FALSE_ALARM", "IN_PROGRESS"),
    ("CANCELLED", "CLOSED"),
)

HUMAN_ONLY = "'CANCELLED', 'CLOSED', 'FALSE_ALARM', 'IN_PROGRESS', 'RESOLVED'"

# (name, source table, local columns, referent table, remote columns)
COMPOSITE_FKS: tuple[tuple[str, str, list[str], str, list[str]], ...] = (
    ("fk_incidents_resolved_by_same_org", "incidents",
     ["organisation_id", "resolved_by_user_id"], "users", ["organisation_id", "id"]),
    ("fk_incidents_closed_by_same_org", "incidents",
     ["organisation_id", "closed_by_user_id"], "users", ["organisation_id", "id"]),
    ("fk_incidents_escalation_policy_same_org", "incidents",
     ["organisation_id", "escalation_policy_id"], "escalation_policies", ["organisation_id", "id"]),
    ("fk_incidents_source_receipt_same_org", "incidents",
     ["organisation_id", "source_receipt_id"], "device_event_receipts", ["organisation_id", "id"]),
    ("fk_incident_events_actor_same_org", "incident_events",
     ["organisation_id", "actor_user_id"], "users", ["organisation_id", "id"]),
    ("fk_device_event_receipts_device_same_org", "device_event_receipts",
     ["organisation_id", "device_id"], "devices", ["organisation_id", "id"]),
    ("fk_device_event_receipts_incident_same_org", "device_event_receipts",
     ["organisation_id", "incident_id"], "incidents", ["organisation_id", "id"]),
    ("fk_calls_trusted_contact_same_org", "calls",
     ["organisation_id", "trusted_contact_id"], "trusted_contacts", ["organisation_id", "id"]),
    ("fk_calls_service_user_same_org", "calls",
     ["organisation_id", "service_user_id"], "service_users", ["organisation_id", "id"]),
)

# Single-column foreign keys from 0001 replaced by the composite ones above.
LEGACY_FKS: tuple[tuple[str, str, str, str], ...] = (
    ("fk_incidents_resolved_by_user_id_users", "incidents", "resolved_by_user_id", "users"),
    ("fk_incidents_closed_by_user_id_users", "incidents", "closed_by_user_id", "users"),
    ("fk_incidents_escalation_policy_id_escalation_policies", "incidents",
     "escalation_policy_id", "escalation_policies"),
    ("fk_incidents_source_receipt_id_device_event_receipts", "incidents",
     "source_receipt_id", "device_event_receipts"),
    ("fk_incident_events_actor_user_id_users", "incident_events", "actor_user_id", "users"),
    ("fk_device_event_receipts_device_id_devices", "device_event_receipts", "device_id", "devices"),
    ("fk_device_event_receipts_incident_id_incidents", "device_event_receipts",
     "incident_id", "incidents"),
    ("fk_calls_trusted_contact_id_trusted_contacts", "calls",
     "trusted_contact_id", "trusted_contacts"),
    ("fk_calls_service_user_id_service_users", "calls", "service_user_id", "service_users"),
)

CHECKS: tuple[tuple[str, str, str], ...] = (
    ("ck_incidents_resolution_recorded", "incidents",
     "status NOT IN ('RESOLVED', 'FALSE_ALARM') OR (resolved_at IS NOT NULL "
     "AND resolved_by_user_id IS NOT NULL AND resolution_category IS NOT NULL)"),
    ("ck_incidents_closure_recorded", "incidents",
     "status <> 'CLOSED' OR (closed_at IS NOT NULL AND closed_by_user_id IS NOT NULL)"),
    ("ck_incidents_event_sequence_non_negative", "incidents", "last_event_sequence >= 0"),
    ("ck_incident_events_sequence_positive", "incident_events", "sequence > 0"),
    ("ck_incident_events_from_status_requires_to_status", "incident_events",
     "from_status IS NULL OR to_status IS NOT NULL"),
    ("ck_incident_events_user_actor_identified", "incident_events",
     "(actor_type = 'USER') = (actor_user_id IS NOT NULL)"),
    ("ck_incident_events_human_only_outcomes", "incident_events",
     f"to_status IS NULL OR to_status NOT IN ({HUMAN_ONLY}) OR actor_type = 'USER'"),
    ("ck_incident_events_ai_cannot_change_status", "incident_events",
     "actor_type <> 'AI' OR to_status IS NULL"),
    ("ck_incident_assignments_released_after_assigned", "incident_assignments",
     "released_at IS NULL OR released_at >= assigned_at"),
    ("ck_device_event_receipts_outcome_has_device", "device_event_receipts",
     "(outcome IS NULL) = (device_id IS NULL)"),
    ("ck_device_event_receipts_incident_outcome_linked", "device_event_receipts",
     "CASE WHEN outcome IN ('INCIDENT_CREATED', 'ATTACHED_TO_INCIDENT') "
     "THEN incident_id IS NOT NULL ELSE incident_id IS NULL END"),
    ("ck_scheduled_actions_attempts_valid", "scheduled_actions",
     "attempts >= 0 AND max_attempts >= 1"),
    ("ck_scheduled_actions_running_has_lease", "scheduled_actions",
     "status <> 'RUNNING' OR (lease_expires_at IS NOT NULL AND locked_by IS NOT NULL)"),
)

TIMELINE_FUNCTIONS: tuple[str, ...] = (
    """
CREATE OR REPLACE FUNCTION careos_assert_incident_timeline(p_incident_id uuid) RETURNS void AS $$
DECLARE
    v_status varchar;
    v_previous varchar := NULL;
    v_first boolean := true;
    v_event record;
BEGIN
    SELECT status INTO v_status FROM incidents WHERE id = p_incident_id;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    FOR v_event IN
        SELECT sequence, from_status, to_status
        FROM incident_events
        WHERE incident_id = p_incident_id AND to_status IS NOT NULL
        ORDER BY sequence
    LOOP
        IF v_first THEN
            IF v_event.from_status IS NOT NULL OR v_event.to_status <> 'RECEIVED' THEN
                RAISE EXCEPTION 'CareOS: incident % timeline must start in RECEIVED (event %)',
                    p_incident_id, v_event.sequence
                    USING ERRCODE = 'check_violation', CONSTRAINT = 'incident_status_timeline';
            END IF;
            v_first := false;
        ELSIF v_event.from_status IS DISTINCT FROM v_previous OR NOT EXISTS (
            SELECT 1 FROM incident_status_transitions t
            WHERE t.from_status = v_event.from_status AND t.to_status = v_event.to_status
        ) THEN
            RAISE EXCEPTION 'CareOS: incident % illegal status change % -> % (event %)',
                p_incident_id, v_event.from_status, v_event.to_status, v_event.sequence
                USING ERRCODE = 'check_violation', CONSTRAINT = 'incident_status_timeline';
        END IF;
        v_previous := v_event.to_status;
    END LOOP;
    IF v_previous IS DISTINCT FROM v_status THEN
        RAISE EXCEPTION 'CareOS: incident % status % is not the last status in its timeline (%)',
            p_incident_id, v_status, COALESCE(v_previous, 'none')
            USING ERRCODE = 'check_violation', CONSTRAINT = 'incident_status_recorded';
    END IF;
END;
$$ LANGUAGE plpgsql
""",
    """
CREATE OR REPLACE FUNCTION careos_check_incident_row() RETURNS trigger AS $$
BEGIN
    PERFORM careos_assert_incident_timeline(NEW.id);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql
""",
    """
CREATE OR REPLACE FUNCTION careos_check_incident_event_row() RETURNS trigger AS $$
BEGIN
    PERFORM careos_assert_incident_timeline(NEW.incident_id);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql
""",
)


def upgrade() -> None:
    transitions = op.create_table(
        "incident_status_transitions",
        sa.Column("from_status", sa.String(length=40), nullable=False),
        sa.Column("to_status", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint(
            "from_status", "to_status", name=op.f("pk_incident_status_transitions")
        ),
    )
    op.bulk_insert(transitions, [{"from_status": f, "to_status": t} for f, t in TRANSITIONS])

    op.create_unique_constraint(
        "uq_device_event_receipts_organisation_id_id",
        "device_event_receipts",
        ["organisation_id", "id"],
    )
    op.create_unique_constraint(
        "uq_trusted_contacts_organisation_id_id", "trusted_contacts", ["organisation_id", "id"]
    )
    for name, table, _column, _referent in LEGACY_FKS:
        op.drop_constraint(op.f(name), table, type_="foreignkey")
    for name, table, local, referent, remote in COMPOSITE_FKS:
        op.create_foreign_key(name, table, referent, local, remote, ondelete="RESTRICT")
    op.create_index(
        "uq_incidents_source_receipt_id",
        "incidents",
        ["source_receipt_id"],
        unique=True,
        postgresql_where=sa.text("source_receipt_id IS NOT NULL"),
    )
    for name, table, condition in CHECKS:
        op.create_check_constraint(op.f(name), table, condition)

    for statement in TIMELINE_FUNCTIONS:
        op.execute(statement)
    op.execute(
        "CREATE CONSTRAINT TRIGGER incidents_status_recorded "
        "AFTER INSERT OR UPDATE OF status ON incidents "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION careos_check_incident_row()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER incident_events_status_chain "
        "AFTER INSERT ON incident_events "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (NEW.to_status IS NOT NULL) "
        "EXECUTE FUNCTION careos_check_incident_event_row()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS incident_events_status_chain ON incident_events")
    op.execute("DROP TRIGGER IF EXISTS incidents_status_recorded ON incidents")
    op.execute("DROP FUNCTION IF EXISTS careos_check_incident_event_row()")
    op.execute("DROP FUNCTION IF EXISTS careos_check_incident_row()")
    op.execute("DROP FUNCTION IF EXISTS careos_assert_incident_timeline(uuid)")
    for name, table, _condition in reversed(CHECKS):
        op.drop_constraint(op.f(name), table, type_="check")
    op.drop_index(
        "uq_incidents_source_receipt_id",
        table_name="incidents",
        postgresql_where=sa.text("source_receipt_id IS NOT NULL"),
    )
    for name, table, _local, _referent, _remote in reversed(COMPOSITE_FKS):
        op.drop_constraint(name, table, type_="foreignkey")
    for name, table, column, referent in LEGACY_FKS:
        op.create_foreign_key(op.f(name), table, referent, [column], ["id"], ondelete="RESTRICT")
    op.drop_constraint("uq_trusted_contacts_organisation_id_id", "trusted_contacts", type_="unique")
    op.drop_constraint(
        "uq_device_event_receipts_organisation_id_id", "device_event_receipts", type_="unique"
    )
    op.drop_table("incident_status_transitions")
