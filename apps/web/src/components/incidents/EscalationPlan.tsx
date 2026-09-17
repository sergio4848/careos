import type { ScheduledActionView } from "@careos/contracts";

import { Badge } from "@/components/ui/Badge";
import { formatClockTime, humanise } from "@/lib/format";

import { Countdown } from "./ElapsedTimer";
import styles from "./EscalationPlan.module.css";

const TONE: Record<ScheduledActionView["status"], "critical" | "warning" | "success" | "info" | "neutral"> = {
  PENDING: "info",
  RUNNING: "warning",
  COMPLETED: "success",
  FAILED: "critical",
  CANCELLED: "neutral",
  SKIPPED: "neutral",
};

export function EscalationPlan({ steps }: { steps: ScheduledActionView[] }) {
  return (
    <ol className={styles.plan}>
      {steps.map((step) => (
        <li key={step.id} className={styles.step}>
          <span className={`${styles.offset} tabular`}>T+{step.delay_seconds}s</span>
          <div className={styles.body}>
            <span className={styles.label}>{step.label}</span>
            <span className={styles.when}>
              {step.status === "PENDING" ? (
                <Countdown to={step.due_at} />
              ) : step.completed_at ? (
                `at ${formatClockTime(step.completed_at)}`
              ) : null}
              {step.attempts > 1 ? ` · ${step.attempts} attempts` : ""}
            </span>
          </div>
          <Badge tone={TONE[step.status]}>{humanise(step.status)}</Badge>
        </li>
      ))}
    </ol>
  );
}
