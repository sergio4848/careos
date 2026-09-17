"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";

import { EscalationPlan } from "@/components/incidents/EscalationPlan";
import { ElapsedTimer } from "@/components/incidents/ElapsedTimer";
import { Timeline } from "@/components/incidents/Timeline";
import { describeActionError, useIncidentActions } from "@/components/incidents/useIncidentActions";
import { PriorityBadge, StatusBadge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { EmptyState, ErrorNotice, Panel } from "@/components/ui/Panel";
import { useAuditLogs, useCloseIncident, useIncident, useTimeline } from "@/lib/api/queries";
import { useSession } from "@/lib/auth/SessionProvider";
import { formatDateTime, humanise } from "@/lib/format";

import styles from "../../page.module.css";

export default function IncidentPage() {
  const { id } = useParams<{ id: string }>();
  const { can } = useSession();
  const incident = useIncident(id);
  const timeline = useTimeline(id);
  const audit = useAuditLogs(id, can("audit:read"));
  const actions = useIncidentActions();
  const close = useCloseIncident();
  const [closeError, setCloseError] = useState<string | null>(null);

  if (incident.error) return <ErrorNotice error={incident.error} />;
  if (!incident.data) return <EmptyState>Loading incident…</EmptyState>;
  const data = incident.data;
  const critical = data.priority === "CRITICAL" && data.is_active;
  const location = [
    data.service_user?.city,
    data.location ? `${data.location.latitude.toFixed(4)}, ${data.location.longitude.toFixed(4)}` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <div className={styles.stack}>
      <p>
        <Link href="/dashboard">← Back to operations</Link>
      </p>

      <header className={styles.pageHeader}>
        <div>
          <p className="eyebrow">
            Incident <span className={styles.mono}>{data.reference}</span>
          </p>
          <h1>
            {humanise(data.trigger_type)} · {data.service_user?.display_name ?? "Unassigned device"}
          </h1>
        </div>
        <div className={styles.headerActions} role="group" aria-label="Incident actions">
          {actions.canTakeOver(data) ? (
            <Button
              variant="danger"
              size="lg"
              onClick={() => actions.startTakeOver(data)}
              loading={actions.takingOverId === data.id}
            >
              Take over
            </Button>
          ) : null}
          {actions.canResolve(data) ? (
            <Button variant="primary" size="lg" onClick={() => actions.openResolve(data)}>
              Resolve incident
            </Button>
          ) : null}
          {data.allowed_actions.includes("close") ? (
            <Button
              variant="secondary"
              size="lg"
              loading={close.isPending}
              onClick={() =>
                close.mutate(
                  { incidentId: data.id },
                  {
                    onError: (error) => setCloseError(describeActionError(error)),
                    onSuccess: () => setCloseError(null),
                  },
                )
              }
            >
              Close incident
            </Button>
          ) : null}
        </div>
      </header>

      {actions.notice || closeError ? (
        <p role="alert" className={styles.notice}>
          {actions.notice ?? closeError}
        </p>
      ) : null}

      <Panel id="incident-summary" title="Incident">
        <dl className={styles.summaryGrid}>
          <div>
            <dt>Service user</dt>
            <dd>
              {data.service_user ? (
                <Link href={`/service-users/${data.service_user.id}`}>{data.service_user.display_name}</Link>
              ) : (
                "Not assigned"
              )}
            </dd>
          </div>
          <div>
            <dt>Device</dt>
            <dd className={styles.mono}>
              {data.device ? `${data.device.external_id} · ${humanise(data.device.device_type)}` : "—"}
            </dd>
          </div>
          <div>
            <dt>Location</dt>
            <dd>{location || "—"}</dd>
          </div>
          <div>
            <dt>Priority</dt>
            <dd>
              <PriorityBadge priority={data.priority} />
            </dd>
          </div>
          <div>
            <dt>Current status</dt>
            <dd>
              <StatusBadge status={data.status} />
            </dd>
          </div>
          <div>
            <dt>Assigned operator</dt>
            <dd>{data.assignee?.name ?? <span className="muted">Unassigned</span>}</dd>
          </div>
          <div>
            <dt>{data.is_active ? "Elapsed time" : "Time to resolution"}</dt>
            <dd>
              <ElapsedTimer
                since={data.created_at}
                until={data.resolved_at}
                className={`${styles.bigTimer} ${critical ? styles.critical : ""}`}
              />
            </dd>
          </div>
          <div>
            <dt>Received</dt>
            <dd>{formatDateTime(data.created_at)}</dd>
          </div>
        </dl>
      </Panel>

      {data.resolution_category ? (
        <Panel id="resolution" title="Resolution">
          <dl className={styles.definition}>
            <dt>Category</dt>
            <dd>{humanise(data.resolution_category)}</dd>
            <dt>Resolved by</dt>
            <dd>{data.resolved_by?.name ?? "—"}</dd>
            <dt>Resolved at</dt>
            <dd>{data.resolved_at ? formatDateTime(data.resolved_at) : "—"}</dd>
            <dt>Notes</dt>
            <dd style={{ whiteSpace: "pre-wrap" }}>{data.resolution_notes ?? "—"}</dd>
            {data.closed_at ? (
              <>
                <dt>Closed</dt>
                <dd>
                  {formatDateTime(data.closed_at)} by {data.closed_by?.name}
                </dd>
              </>
            ) : null}
          </dl>
        </Panel>
      ) : null}

      <div className={styles.twoColumn}>
        <Panel id="timeline" title="Timeline">
          {timeline.data ? (
            <Timeline events={timeline.data} startedAt={data.created_at} />
          ) : (
            <EmptyState>Loading timeline…</EmptyState>
          )}
        </Panel>
        <div className={styles.column}>
          <Panel id="escalation" title="Escalation plan (mock providers)">
            {data.escalation.length ? <EscalationPlan steps={data.escalation} /> : <EmptyState>No steps.</EmptyState>}
          </Panel>
          {can("audit:read") ? (
            <Panel id="incident-audit" title="Audit trail">
              {audit.data && audit.data.length > 0 ? (
                <table className={styles.table}>
                  <thead>
                    <tr>
                      <th scope="col">Time</th>
                      <th scope="col">Action</th>
                      <th scope="col">Actor</th>
                    </tr>
                  </thead>
                  <tbody>
                    {audit.data.map((entry) => (
                      <tr key={entry.id}>
                        <td className="tabular">{formatDateTime(entry.created_at)}</td>
                        <td className={styles.mono}>{entry.action}</td>
                        <td>{entry.actor_name ?? humanise(entry.actor_type)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <EmptyState>No audit records yet.</EmptyState>
              )}
            </Panel>
          ) : null}
        </div>
      </div>
      {actions.dialog}
    </div>
  );
}
