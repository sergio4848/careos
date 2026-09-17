"use client";

import type { IncidentSummary } from "@careos/contracts";
import Link from "next/link";

import { IncidentCard } from "@/components/incidents/IncidentCard";
import { useIncidentActions } from "@/components/incidents/useIncidentActions";
import { PriorityBadge, StatusBadge } from "@/components/ui/Badge";
import { EmptyState, ErrorNotice, Panel } from "@/components/ui/Panel";
import { useIncidents } from "@/lib/api/queries";
import { formatDateTime, humanise } from "@/lib/format";

import styles from "./IncidentBoard.module.css";

export function IncidentBoard() {
  const active = useIncidents("active");
  const awaiting = useIncidents("awaiting_closure");
  const actions = useIncidentActions();

  return (
    <div className={styles.layout}>
      <Panel
        id="live-incidents"
        title={`Live incidents${active.data ? ` (${active.data.length})` : ""}`}
      >
        {actions.notice ? (
          <p role="alert" className={styles.notice}>
            {actions.notice}
            <button type="button" onClick={actions.clearNotice} aria-label="Dismiss message">
              ×
            </button>
          </p>
        ) : null}
        {active.error ? <ErrorNotice error={active.error} /> : null}
        {active.isPending ? <EmptyState>Loading live incidents…</EmptyState> : null}
        {active.data && active.data.length === 0 ? (
          <EmptyState>No active incidents. New alarms appear here instantly.</EmptyState>
        ) : null}
        <div className={styles.grid}>
          {active.data?.map((incident) => (
            <IncidentCard
              key={incident.id}
              incident={incident}
              canTakeOver={actions.canTakeOver(incident)}
              takingOver={actions.takingOverId === incident.id}
              onTakeOver={actions.startTakeOver}
            />
          ))}
        </div>
      </Panel>

      <Panel id="awaiting-closure" title="Resolved · awaiting closure">
        {awaiting.data && awaiting.data.length === 0 ? <EmptyState>Nothing awaiting closure.</EmptyState> : null}
        <ul className={styles.list}>
          {awaiting.data?.map((incident: IncidentSummary) => (
            <li key={incident.id} className={styles.row}>
              <div className={styles.rowMain}>
                <Link href={`/incidents/${incident.id}`} className={styles.rowTitle}>
                  {incident.service_user?.display_name ?? incident.reference}
                </Link>
                <span className="muted">
                  {humanise(incident.trigger_type)} · resolved {incident.resolved_at ? formatDateTime(incident.resolved_at) : ""}
                </span>
              </div>
              <div className={styles.rowBadges}>
                <PriorityBadge priority={incident.priority} />
                <StatusBadge status={incident.status} />
              </div>
            </li>
          ))}
        </ul>
      </Panel>
    </div>
  );
}
