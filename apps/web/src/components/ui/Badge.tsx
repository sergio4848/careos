import type { IncidentPriority, IncidentStatus } from "@careos/contracts";

import { humanise } from "@/lib/format";

import styles from "./Badge.module.css";

type Tone = "critical" | "warning" | "success" | "info" | "neutral";

export function Badge({ tone, children, strong = false }: { tone: Tone; children: React.ReactNode; strong?: boolean }) {
  return <span className={`${styles.badge} ${styles[tone]} ${strong ? styles.strong : ""}`}>{children}</span>;
}

const PRIORITY_TONE: Record<IncidentPriority, Tone> = {
  CRITICAL: "critical",
  HIGH: "warning",
  MEDIUM: "info",
  LOW: "neutral",
};

export function PriorityBadge({ priority }: { priority: IncidentPriority }) {
  return (
    <Badge tone={PRIORITY_TONE[priority]} strong={priority === "CRITICAL"}>
      <span className="visually-hidden">Priority </span>
      {priority}
    </Badge>
  );
}

export function statusTone(status: IncidentStatus): Tone {
  switch (status) {
    case "ESCALATED":
    case "FAILED":
      return "critical";
    case "OPEN":
    case "CONTACTING":
    case "RECEIVED":
    case "VALIDATING":
    case "DEVICE_ERROR":
      return "warning";
    case "ACKNOWLEDGED":
    case "IN_PROGRESS":
      return "info";
    case "RESOLVED":
    case "FALSE_ALARM":
    case "CLOSED":
      return "success";
    default:
      return "neutral";
  }
}

export function StatusBadge({ status }: { status: IncidentStatus }) {
  return (
    <Badge tone={statusTone(status)}>
      <span className="visually-hidden">Status </span>
      {humanise(status)}
    </Badge>
  );
}
