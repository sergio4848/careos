"use client";

import { IncidentBoard } from "@/components/dashboard/IncidentBoard";
import { StatsBar } from "@/components/dashboard/StatsBar";
import { useDashboardSummary } from "@/lib/api/queries";

import styles from "../page.module.css";

export default function DashboardPage() {
  const summary = useDashboardSummary();
  return (
    <div className={styles.stack}>
      <header className={styles.pageHeader}>
        <div>
          <p className="eyebrow">Real-time operations</p>
          <h1>Incident operations</h1>
        </div>
      </header>
      <StatsBar summary={summary.data} />
      <IncidentBoard />
    </div>
  );
}
