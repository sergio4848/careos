"use client";

import { describeElapsed, formatElapsed } from "@/lib/format";
import { useNow } from "@/lib/time/use-now";

/**
 * Live "time since alarm" counter. Uses the shared ticker and the server-corrected clock,
 * so every card on the board ticks in sync and a wrong PC clock cannot hide an old alarm.
 */
export function ElapsedTimer({
  since,
  until,
  className,
}: {
  since: string;
  /** Freeze the timer (e.g. at resolution time). */
  until?: string | null;
  className?: string;
}) {
  const now = useNow();
  const start = Date.parse(since);
  const end = until ? Date.parse(until) : now;
  const elapsed = now === 0 && !until ? 0 : end - start;
  return (
    <time
      className={`tabular ${className ?? ""}`}
      dateTime={since}
      title={`Alarm received ${new Date(since).toLocaleString("en-GB")}`}
      aria-label={`Elapsed ${describeElapsed(elapsed)}`}
    >
      {formatElapsed(elapsed)}
    </time>
  );
}

export function Countdown({ to }: { to: string }) {
  const now = useNow();
  const remaining = Date.parse(to) - now;
  if (now === 0) return null;
  return <span className="tabular">{remaining > 0 ? `in ${formatElapsed(remaining)}` : "due now"}</span>;
}
