"use client";

import { Button } from "@/components/ui/Button";
import { useApiHealth } from "@/lib/providers";
import { useRealtimeStatus } from "@/lib/realtime/RealtimeProvider";
import { systemNotice, type SystemNotice } from "@/lib/status/system-status";
import { useLocalNow } from "@/lib/time/use-now";

import styles from "./StatusBanner.module.css";

export function SystemStatusBannerView({ notice, onRetry }: { notice: SystemNotice | null; onRetry: () => void }) {
  if (!notice) return null;
  return (
    <div
      className={`${styles.banner} ${styles[notice.severity]}`}
      role={notice.severity === "critical" ? "alert" : "status"}
    >
      <span className={styles.title}>{notice.title}</span>
      <span className={styles.detail}>{notice.detail}</span>
      {notice.canRetry ? (
        <Button size="sm" onClick={onRetry}>
          Reconnect now
        </Button>
      ) : null}
    </div>
  );
}

/** Console-wide health of the operator's own connection to CareOS (API and live updates). */
export function SystemStatusBanner() {
  const api = useApiHealth();
  const realtime = useRealtimeStatus();
  const now = useLocalNow(); // ticks every second so the reconnect countdown stays accurate
  return <SystemStatusBannerView notice={systemNotice(api, realtime, now)} onRetry={realtime.retryNow} />;
}
