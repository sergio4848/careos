import type { ApiHealth } from "../api/health";
import type { ConnectionSnapshot } from "../realtime/connection";

export interface SystemNotice {
  severity: "critical" | "warning";
  title: string;
  detail: string;
  /** Offer a "Reconnect now" action. */
  canRetry: boolean;
}

/**
 * The single most important thing an operator must know about the console's own health.
 * API down outranks realtime down: without the API nothing on screen can be trusted.
 */
export function systemNotice(api: ApiHealth, realtime: ConnectionSnapshot, now: number): SystemNotice | null {
  if (api.status === "unavailable") {
    return {
      severity: "critical",
      title: "CareOS server unreachable",
      detail:
        "Incident information on screen may be out of date and actions may fail. Retrying automatically. " +
        "Follow your contingency procedure for handling alarms until this clears.",
      canRetry: false,
    };
  }
  if (realtime.status === "disconnected") {
    const seconds = realtime.nextRetryAt === null ? null : Math.max(0, Math.ceil((realtime.nextRetryAt - now) / 1000));
    return {
      severity: "warning",
      title: "Live updates interrupted",
      detail:
        "New alarms still appear: the board refreshes from the server every 10 seconds." +
        (seconds === null ? "" : ` Reconnecting in ${seconds} s.`),
      canRetry: true,
    };
  }
  if (realtime.status === "reconnecting") {
    return {
      severity: "warning",
      title: "Reconnecting to live updates…",
      detail: "The board refreshes from the server every 10 seconds until the connection is back.",
      canRetry: false,
    };
  }
  return null; // live, first connection attempt, or unauthorised (the session flow takes over)
}

export interface Freshness {
  stale: boolean;
  label: string;
}

/** How old the data on screen is. `updatedAt` is react-query's `dataUpdatedAt` (0 = never). */
export function describeFreshness(updatedAt: number, now: number, staleAfterMs = 30_000): Freshness {
  if (!updatedAt) return { stale: false, label: "Loading…" };
  const ageMs = Math.max(0, now - updatedAt);
  const seconds = Math.floor(ageMs / 1000);
  const age = seconds < 5 ? "just now" : seconds < 60 ? `${seconds} s ago` : `${Math.floor(seconds / 60)} min ago`;
  const stale = ageMs > staleAfterMs;
  return { stale, label: stale ? `Data may be out of date · last updated ${age}` : `Updated ${age}` };
}
