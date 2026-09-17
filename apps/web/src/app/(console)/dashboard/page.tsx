"use client";

import { EscalationDelayBanner, FreshnessIndicator } from "@/components/dashboard/DataHealth";
import { IncidentBoard } from "@/components/dashboard/IncidentBoard";
import { StatsBar } from "@/components/dashboard/StatsBar";
import { useDashboardSummary, useIncidents } from "@/lib/api/queries";

import styles from "../page.module.css";

export default function DashboardPage() {
  const summary = useDashboardSummary();
  const active = useIncidents("active");
  // The board is only as fresh as its oldest successful refresh.
  const updatedAt = Math.min(summary.dataUpdatedAt || Infinity, active.dataUpdatedAt || Infinity);
  return (
    <div className={styles.stack}>
      <header className={styles.pageHeader}>
        <div>
          <p className="eyebrow">Real-time operations</p>
          <h1>Incident operations</h1>
        </div>
        <FreshnessIndicator updatedAt={Number.isFinite(updatedAt) ? updatedAt : 0} />
      </header>
      <EscalationDelayBanner overdue={summary.data?.escalation_overdue} />
      <StatsBar summary={summary.data} />
      <IncidentBoard />
    </div>
  );
}
