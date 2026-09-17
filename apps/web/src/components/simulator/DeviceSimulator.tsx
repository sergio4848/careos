"use client";

import type { DeviceView, SimulatorEventRequest, SimulatorEventResult } from "@careos/contracts";
import Link from "next/link";
import { useState } from "react";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { isApiError } from "@/lib/api/client";
import { useSimulateEvent } from "@/lib/api/queries";
import { humanise } from "@/lib/format";

import styles from "./DeviceSimulator.module.css";

type EventType = NonNullable<SimulatorEventRequest["event_type"]>;
const EVENT_TYPES: EventType[] = ["SOS_BUTTON", "FALL_DETECTED", "DEVICE_FAULT", "LOW_BATTERY", "HEARTBEAT"];

interface GatewayResponse {
  duplicate?: boolean;
  outcome?: string;
  incident_id?: string;
  error?: { code?: string; message?: string };
}

export function DeviceSimulator({ device }: { device: DeviceView }) {
  const simulate = useSimulateEvent();
  const [eventType, setEventType] = useState<EventType>("SOS_BUTTON");
  const [battery, setBattery] = useState(device.connection?.battery_level ?? 84);
  const [signal, setSignal] = useState(device.connection?.signal_strength ?? 92);
  const [last, setLast] = useState<SimulatorEventResult | null>(null);

  const send = (request: SimulatorEventRequest) =>
    simulate.mutate({ deviceId: device.id, request }, { onSuccess: (result) => setLast(result) });

  const gateway = last?.gateway_response as GatewayResponse | undefined;
  const name = device.service_user?.display_name ?? "Unassigned device";
  const status = device.connection?.status ?? "UNKNOWN";
  const titleId = `sim-${device.id}`;

  return (
    <article className={styles.card} aria-labelledby={titleId}>
      <header className={styles.header}>
        <div>
          <h2 id={titleId}>{name}</h2>
          <p className="muted">{device.service_user?.city ?? "—"}</p>
        </div>
      </header>

      <dl className={styles.facts}>
        <div>
          <dt>Device</dt>
          <dd className={styles.mono}>{device.external_id}</dd>
        </div>
        <div>
          <dt>Battery</dt>
          <dd>{battery}%</dd>
        </div>
        <div>
          <dt>Signal</dt>
          <dd>{signal}%</dd>
        </div>
        <div>
          <dt>Status</dt>
          <dd>
            <Badge tone={status === "ONLINE" ? "success" : "warning"}>{status}</Badge>
          </dd>
        </div>
      </dl>

      <Button
        variant="danger"
        size="lg"
        className={styles.sos}
        loading={simulate.isPending}
        onClick={() => send({ event_type: eventType, battery, signal })}
      >
        {eventType === "SOS_BUTTON" ? "Send test SOS" : `Send ${humanise(eventType)}`}
      </Button>

      <details className={styles.advanced}>
        <summary>Simulation options</summary>
        <div className={styles.controls}>
          <label>
            <span>Event</span>
            <select value={eventType} onChange={(event) => setEventType(event.target.value as EventType)}>
              {EVENT_TYPES.map((type) => (
                <option key={type} value={type}>
                  {humanise(type)}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Battery {battery}%</span>
            <input type="range" min={0} max={100} value={battery} onChange={(e) => setBattery(Number(e.target.value))} />
          </label>
          <label>
            <span>Signal {signal}%</span>
            <input type="range" min={0} max={100} value={signal} onChange={(e) => setSignal(Number(e.target.value))} />
          </label>
        </div>
        <Button
          variant="secondary"
          disabled={!last || simulate.isPending}
          onClick={() =>
            last &&
            send({
              event_type: eventType,
              event_id: last.event_id,
              timestamp_ms: Number(last.vendor_payload.ts_ms),
              battery: Number(last.vendor_payload.bat_pct),
              signal: Number(last.vendor_payload.rssi_pct),
            })
          }
          title="Replays the identical device message to demonstrate gateway idempotency"
        >
          Resend last event
        </Button>
      </details>

      {simulate.error ? (
        <p role="alert" className={styles.error}>
          {isApiError(simulate.error) ? simulate.error.message : "Simulator request failed."}
        </p>
      ) : null}

      {last ? (
        <section className={styles.result} aria-live="polite" aria-label="Gateway response">
          <p>
            <strong>Gateway {last.gateway_status_code}</strong>
            {gateway?.duplicate ? " · duplicate event, no new incident" : ""}
            {gateway?.outcome ? ` · ${humanise(gateway.outcome)}` : ""}
            {gateway?.error?.message ? ` · ${gateway.error.message}` : ""}
            {gateway?.incident_id ? (
              <>
                {" · "}
                <Link href={`/incidents/${gateway.incident_id}`}>Open incident</Link>
              </>
            ) : null}
          </p>
          <details>
            <summary>Vendor payload sent to the Device Gateway</summary>
            <pre>{JSON.stringify(last.vendor_payload, null, 2)}</pre>
          </details>
          <details>
            <summary>Gateway response</summary>
            <pre>{JSON.stringify(last.gateway_response, null, 2)}</pre>
          </details>
        </section>
      ) : null}
    </article>
  );
}
