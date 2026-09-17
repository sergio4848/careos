"use client";

import { DeviceSimulator } from "@/components/simulator/DeviceSimulator";
import { EmptyState, ErrorNotice } from "@/components/ui/Panel";
import { useDevices } from "@/lib/api/queries";
import { useConfig } from "@/lib/config";

import styles from "../../page.module.css";

export default function SimulatorPage() {
  const { simulatorEnabled } = useConfig();
  const devices = useDevices();

  if (!simulatorEnabled) return <EmptyState>The SOS simulator is disabled in this environment.</EmptyState>;

  return (
    <div className={styles.stack}>
      <header className={styles.pageHeader}>
        <div>
          <p className="eyebrow">Development tool</p>
          <h1>SOS Simulator</h1>
          <p className="muted">
            Acts as a manufacturer platform: sends a vendor-format device message over HTTP to the Device Event
            Gateway, authenticated with a gateway credential. It never creates incidents directly.
          </p>
        </div>
      </header>
      {devices.error ? <ErrorNotice error={devices.error} /> : null}
      {devices.data?.length === 0 ? <EmptyState>No devices registered.</EmptyState> : null}
      <div style={{ display: "grid", gap: "1rem", gridTemplateColumns: "repeat(auto-fill, minmax(360px, 1fr))" }}>
        {devices.data?.map((device) => <DeviceSimulator key={device.id} device={device} />)}
      </div>
    </div>
  );
}
