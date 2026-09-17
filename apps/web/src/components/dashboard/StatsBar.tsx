"use client";

import type { DashboardSummary } from "@careos/contracts";

import styles from "./StatsBar.module.css";

type Tone = "critical" | "warning" | "ok" | "neutral";

interface Stat {
  key: keyof DashboardSummary;
  label: string;
  tone: (value: number) => Tone;
}

const STATS: Stat[] = [
  { key: "active_incidents", label: "Active incidents", tone: (v) => (v > 0 ? "warning" : "ok") },
  { key: "critical", label: "Critical", tone: (v) => (v > 0 ? "critical" : "ok") },
  { key: "unacknowledged", label: "Unacknowledged", tone: (v) => (v > 0 ? "critical" : "ok") },
  { key: "devices_online", label: "Devices online", tone: () => "neutral" },
  { key: "devices_offline", label: "Devices offline", tone: (v) => (v > 0 ? "warning" : "ok") },
];

export function StatsBar({ summary }: { summary: DashboardSummary | undefined }) {
  return (
    <section aria-label="Operational summary" className={styles.bar}>
      {STATS.map((stat) => {
        const value = summary?.[stat.key];
        const tone = value === undefined ? "neutral" : stat.tone(value);
        return (
          <div key={stat.key} className={`${styles.tile} ${styles[tone]}`}>
            <span className={styles.label}>{stat.label}</span>
            <span className={`${styles.value} tabular`} aria-live={stat.key === "critical" ? "polite" : undefined}>
              {value ?? "–"}
            </span>
          </div>
        );
      })}
    </section>
  );
}
