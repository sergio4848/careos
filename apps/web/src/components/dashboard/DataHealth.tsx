"use client";

import { describeFreshness } from "@/lib/status/system-status";
import { useLocalNow } from "@/lib/time/use-now";

import styles from "../layout/StatusBanner.module.css";

/** How old the board's data is. Stale data is called out, never shown as if it were current. */
export function FreshnessIndicator({ updatedAt, staleAfterMs = 30_000 }: { updatedAt: number; staleAfterMs?: number }) {
  const now = useLocalNow();
  const { stale, label } = describeFreshness(updatedAt, now, staleAfterMs);
  return (
    <p className={`${styles.freshness} ${stale ? styles.stale : ""}`} data-stale={stale}>
      {stale ? <span role="alert" className="visually-hidden">Operations data may be out of date.</span> : null}
      {label}
    </p>
  );
}

/**
 * Escalation steps are overdue: the worker is down or saturated, so automated calls and
 * notifications are not happening on time. Humans must take over.
 */
export function EscalationDelayBanner({ overdue }: { overdue: number | undefined }) {
  if (!overdue) return null;
  return (
    <div className={`${styles.banner} ${styles.inline} ${styles.critical}`} role="alert">
      <span className={styles.title}>Automated escalation is delayed</span>
      <span className={styles.detail}>
        {overdue} escalation {overdue === 1 ? "step is" : "steps are"} overdue. Contact service users and trusted
        contacts manually for open incidents, and report the delay to your technical on-call.
      </span>
    </div>
  );
}
