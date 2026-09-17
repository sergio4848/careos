"use client";

import type { IncidentSummary } from "@careos/contracts";
import Link from "next/link";

import { Button } from "@/components/ui/Button";
import { PriorityBadge, StatusBadge } from "@/components/ui/Badge";
import { humanise } from "@/lib/format";

import { Countdown, ElapsedTimer } from "./ElapsedTimer";
import styles from "./IncidentCard.module.css";

export interface IncidentCardProps {
  incident: IncidentSummary;
  canTakeOver: boolean;
  takingOver?: boolean;
  onTakeOver: (incident: IncidentSummary) => void;
}

export function IncidentCard({ incident, canTakeOver, takingOver, onTakeOver }: IncidentCardProps) {
  const titleId = `incident-${incident.id}-title`;
  const unacknowledged = !incident.acknowledged_at && !incident.assignee;
  const critical = incident.priority === "CRITICAL";
  const name = incident.service_user?.display_name ?? "Unassigned device";
  const classes = [styles.card, critical ? styles.critical : "", unacknowledged ? styles.unacknowledged : ""]
    .filter(Boolean)
    .join(" ");

  return (
    <article className={classes} aria-labelledby={titleId} data-testid="incident-card">
      <div className={styles.strip}>
        <PriorityBadge priority={incident.priority} />
        {unacknowledged ? <span className={styles.flag}>Unacknowledged</span> : null}
        <span className={styles.reference}>{incident.reference}</span>
      </div>

      <div className={styles.headline}>
        <h3 id={titleId} className={styles.name}>
          {name}
        </h3>
        <span className={styles.trigger}>{humanise(incident.trigger_type)}</span>
      </div>

      <dl className={styles.facts}>
        <div>
          <dt>Elapsed</dt>
          <dd className={styles.timer}>
            <ElapsedTimer since={incident.created_at} until={incident.resolved_at} />
          </dd>
        </div>
        <div>
          <dt>Status</dt>
          <dd>
            <StatusBadge status={incident.status} />
          </dd>
        </div>
        <div>
          <dt>Device</dt>
          <dd className={styles.mono}>{incident.device?.external_id ?? "—"}</dd>
        </div>
        <div>
          <dt>Assigned</dt>
          <dd>{incident.assignee ? incident.assignee.name : <span className="muted">Unassigned</span>}</dd>
        </div>
        <div className={styles.wide}>
          <dt>Next escalation step</dt>
          <dd>
            {incident.next_action ? (
              <>
                {incident.next_action.label} · <Countdown to={incident.next_action.due_at} />
              </>
            ) : (
              <span className="muted">None pending</span>
            )}
          </dd>
        </div>
      </dl>

      <div className={styles.actions}>
        <Link className={styles.linkButton} href={`/incidents/${incident.id}`}>
          View incident
        </Link>
        {canTakeOver && !incident.assignee ? (
          <Button variant="danger" onClick={() => onTakeOver(incident)} loading={takingOver}>
            Take over
          </Button>
        ) : null}
      </div>
    </article>
  );
}
