import type { ControlMessage, RealtimeMessage } from "@careos/contracts";

export type QueryKeyPrefix = readonly string[];

/**
 * Map a realtime notification to the cached queries that must be refetched.
 * Messages carry identifiers only; authoritative data always comes from the REST API.
 */
export function queryKeysToInvalidate(message: RealtimeMessage): QueryKeyPrefix[] {
  switch (message.type) {
    case "incident.created":
      return [["incidents"], ["dashboard"]];
    case "incident.updated":
      return [
        ["incidents"],
        ["dashboard"],
        ...(message.incident_id ? [["incident", message.incident_id], ["audit"]] : []),
      ];
    case "device.updated":
      return [["devices"], ["dashboard"]];
    default:
      return [];
  }
}

export function parseSocketMessage(raw: string): RealtimeMessage | ControlMessage | null {
  if (raw === "pong") return null;
  try {
    const value = JSON.parse(raw) as { type?: unknown };
    return typeof value.type === "string" ? (value as RealtimeMessage | ControlMessage) : null;
  } catch {
    return null;
  }
}

export function isRealtimeMessage(message: RealtimeMessage | ControlMessage): message is RealtimeMessage {
  return "organisation_id" in message;
}

/** Exponential backoff with jitter, capped at 30 seconds. */
export function reconnectDelay(attempt: number, random: () => number = Math.random): number {
  const base = Math.min(30_000, 1_000 * 2 ** attempt);
  return Math.round(base * (0.8 + random() * 0.4));
}
