"use client";

import { useMemo, useState } from "react";

import { Badge } from "@/components/ui/Badge";
import { EmptyState, ErrorNotice, Panel } from "@/components/ui/Panel";
import { useAuditLogs } from "@/lib/api/queries";
import { useSession } from "@/lib/auth/SessionProvider";
import { formatDateTime, humanise } from "@/lib/format";

import styles from "../page.module.css";

export default function AuditPage() {
  const { can } = useSession();
  const allowed = can("audit:read");
  const logs = useAuditLogs(undefined, allowed);
  const [filter, setFilter] = useState("");

  const rows = useMemo(
    () => (logs.data ?? []).filter((entry) => !filter || entry.action.includes(filter.trim().toUpperCase())),
    [logs.data, filter],
  );

  if (!allowed) return <EmptyState>Your role does not include access to the audit trail.</EmptyState>;

  return (
    <div className={styles.stack}>
      <header className={styles.pageHeader}>
        <div>
          <p className="eyebrow">Compliance</p>
          <h1>Audit trail</h1>
          <p className="muted">Immutable record of security-relevant and operational actions. Read-only.</p>
        </div>
        <label className={styles.headerActions}>
          <span className="muted">Filter action</span>
          <input
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
            placeholder="e.g. INCIDENT_"
            style={{
              font: "inherit",
              color: "var(--text)",
              background: "var(--bg-raised)",
              border: "1px solid var(--border-strong)",
              borderRadius: 6,
              padding: "0.4rem 0.6rem",
            }}
          />
        </label>
      </header>
      <Panel id="audit" title={`Latest ${rows.length} records`}>
        {logs.error ? <ErrorNotice error={logs.error} /> : null}
        {rows.length === 0 && !logs.isPending ? <EmptyState>No matching records.</EmptyState> : null}
        {rows.length > 0 ? (
          <div style={{ overflowX: "auto" }}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th scope="col">Time</th>
                  <th scope="col">Action</th>
                  <th scope="col">Outcome</th>
                  <th scope="col">Actor</th>
                  <th scope="col">Resource</th>
                  <th scope="col">Request ID</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((entry) => (
                  <tr key={entry.id}>
                    <td className="tabular">{formatDateTime(entry.created_at)}</td>
                    <td className={styles.mono}>{entry.action}</td>
                    <td>
                      <Badge tone={entry.outcome === "SUCCESS" ? "success" : entry.outcome === "DENIED" ? "critical" : "warning"}>
                        {entry.outcome}
                      </Badge>
                    </td>
                    <td>{entry.actor_name ?? humanise(entry.actor_type)}</td>
                    <td className={styles.mono}>
                      {entry.resource_type ? `${entry.resource_type}:${entry.resource_id?.slice(0, 8) ?? ""}` : "—"}
                    </td>
                    <td className={styles.mono}>{entry.request_id?.slice(0, 12) ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </Panel>
    </div>
  );
}
