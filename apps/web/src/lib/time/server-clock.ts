/**
 * Operator timers must not depend on the accuracy of the operator's PC clock.
 * The API client feeds the server's `Date` header in; timers read `serverNow()`.
 */
let offsetMs = 0;

export function syncServerClock(dateHeader: string | null, receivedAt: number = Date.now()): void {
  if (!dateHeader) return;
  const serverTime = Date.parse(dateHeader);
  if (Number.isNaN(serverTime)) return;
  // The Date header has one-second resolution; ignore sub-second noise.
  const measured = serverTime - receivedAt;
  if (Math.abs(measured - offsetMs) >= 1000) offsetMs = measured;
}

export function serverNow(): number {
  return Date.now() + offsetMs;
}

export function resetServerClock(): void {
  offsetMs = 0;
}
