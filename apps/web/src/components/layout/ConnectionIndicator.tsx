"use client";

import { useRealtimeStatus, type RealtimeStatus } from "@/lib/realtime/RealtimeProvider";

import styles from "./AppShell.module.css";

const LABELS: Record<RealtimeStatus, { text: string; description: string; tone: string }> = {
  live: { text: "Live", description: "Receiving real-time updates", tone: styles.live ?? "" },
  connecting: { text: "Connecting", description: "Connecting to real-time updates", tone: styles.pending ?? "" },
  reconnecting: {
    text: "Reconnecting · polling",
    description: "Real-time connection lost; refreshing every 10 seconds",
    tone: styles.degraded ?? "",
  },
  unauthorised: { text: "Session expired", description: "Sign in again", tone: styles.degraded ?? "" },
};

export function ConnectionIndicator() {
  const { status } = useRealtimeStatus();
  const label = LABELS[status];
  return (
    <span className={`${styles.connection} ${label.tone}`} role="status" title={label.description}>
      <span className={styles.dot} aria-hidden="true" />
      {label.text}
      <span className="visually-hidden">: {label.description}</span>
    </span>
  );
}
