import type { IncidentEventView } from "@careos/contracts";

import { formatClockTime, formatElapsed, humanise } from "@/lib/format";

import styles from "./Timeline.module.css";

const ACTOR_LABEL: Record<IncidentEventView["actor_type"], string> = {
  SYSTEM: "System",
  DEVICE: "Device",
  USER: "Operator",
  PROVIDER: "Provider",
  AI: "AI assistant",
};

/** Append-only incident history, oldest first (chronological). */
export function Timeline({ events, startedAt }: { events: IncidentEventView[]; startedAt: string }) {
  const start = Date.parse(startedAt);
  return (
    <ol className={styles.timeline} aria-label="Incident timeline">
      {events.map((event) => {
        const advisory = event.actor_type === "AI" && typeof event.data.advisory_summary === "string";
        return (
          <li key={event.id} className={`${styles.item} ${styles[event.actor_type.toLowerCase()] ?? ""}`}>
            <div className={styles.when}>
              <time dateTime={event.occurred_at} className="tabular">
                {formatClockTime(event.occurred_at)}
              </time>
              <span className={`${styles.offset} tabular`}>
                +{formatElapsed(Date.parse(event.occurred_at) - start)}
              </span>
            </div>
            <div className={styles.marker} aria-hidden="true" />
            <div className={styles.content}>
              <div className={styles.meta}>
                <span className={styles.type}>{humanise(event.event_type)}</span>
                <span className={styles.actor}>{event.actor?.name ?? ACTOR_LABEL[event.actor_type]}</span>
                {event.from_status || event.to_status ? (
                  <span className={styles.transition}>
                    {event.from_status ? humanise(event.from_status) : "—"} → {humanise(event.to_status ?? "")}
                  </span>
                ) : null}
              </div>
              <p className={styles.message}>{event.message}</p>
              {advisory ? (
                <aside className={styles.advisory} aria-label="AI-generated advisory note">
                  <strong>AI-generated · advisory only</strong>
                  <p>{String(event.data.advisory_summary)}</p>
                </aside>
              ) : null}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
