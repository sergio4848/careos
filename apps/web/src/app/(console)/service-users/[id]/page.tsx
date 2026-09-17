"use client";

import Link from "next/link";
import { useParams } from "next/navigation";

import { Badge, PriorityBadge, StatusBadge } from "@/components/ui/Badge";
import { EmptyState, ErrorNotice, Panel } from "@/components/ui/Panel";
import { useServiceUser } from "@/lib/api/queries";
import { formatDateTime, humanise } from "@/lib/format";

import styles from "../../page.module.css";

export default function ServiceUserPage() {
  const { id } = useParams<{ id: string }>();
  const profile = useServiceUser(id);

  if (profile.error) return <ErrorNotice error={profile.error} />;
  if (!profile.data) return <EmptyState>Loading profile…</EmptyState>;
  const person = profile.data;

  return (
    <div className={styles.stack}>
      <p>
        <Link href="/dashboard">← Back to operations</Link>
      </p>
      <header className={styles.pageHeader}>
        <div>
          <p className="eyebrow">Service user</p>
          <h1>{person.display_name}</h1>
          <p className="muted">
            {person.city ?? "Location not recorded"}
            {person.external_reference ? ` · Ref ${person.external_reference}` : ""}
          </p>
        </div>
        <Badge tone={person.status === "ACTIVE" ? "success" : "neutral"}>{humanise(person.status)}</Badge>
      </header>

      <div className={styles.twoColumn}>
        <div className={styles.column}>
          <Panel id="contacts" title="Trusted contacts (escalation order)">
            {person.contacts.length === 0 ? <EmptyState>No trusted contacts recorded.</EmptyState> : null}
            <ol className={styles.plainList}>
              {person.contacts.map((contact) => (
                <li key={contact.id} className={styles.listItem}>
                  <div>
                    <strong>
                      #{contact.priority} {contact.full_name}
                    </strong>
                    <div className="muted">{contact.relationship}</div>
                  </div>
                  <div className="tabular">{contact.phone_number}</div>
                </li>
              ))}
            </ol>
          </Panel>
          <Panel id="recent-incidents" title="Recent incidents">
            {person.recent_incidents.length === 0 ? <EmptyState>No incidents.</EmptyState> : null}
            <ul className={styles.plainList}>
              {person.recent_incidents.map((incident) => (
                <li key={incident.id} className={styles.listItem}>
                  <div>
                    <Link href={`/incidents/${incident.id}`}>{humanise(incident.trigger_type)}</Link>
                    <div className="muted">{formatDateTime(incident.created_at)}</div>
                  </div>
                  <div className={styles.headerActions}>
                    <PriorityBadge priority={incident.priority} />
                    <StatusBadge status={incident.status} />
                  </div>
                </li>
              ))}
            </ul>
          </Panel>
        </div>
        <div className={styles.column}>
          <Panel id="details" title="Contact details">
            <dl className={styles.definition}>
              <dt>Phone</dt>
              <dd className="tabular">{person.phone_number ?? "—"}</dd>
              <dt>Address</dt>
              <dd>{[person.address_line1, person.city, person.postcode].filter(Boolean).join(", ") || "—"}</dd>
            </dl>
          </Panel>
          <Panel id="devices" title="Devices">
            {person.devices.length === 0 ? <EmptyState>No devices assigned.</EmptyState> : null}
            <ul className={styles.plainList}>
              {person.devices.map((device) => (
                <li key={device.id} className={styles.listItem}>
                  <div>
                    <strong className={styles.mono}>{device.external_id}</strong>
                    <div className="muted">
                      {humanise(device.device_type)} · {device.manufacturer}
                    </div>
                  </div>
                  <div className={styles.headerActions}>
                    <Badge tone={device.connection?.status === "ONLINE" ? "success" : "warning"}>
                      {device.connection?.status ?? "UNKNOWN"}
                    </Badge>
                    <Badge tone={device.low_battery ? "critical" : "neutral"}>
                      {device.connection?.battery_level ?? "—"}% battery
                    </Badge>
                  </div>
                </li>
              ))}
            </ul>
          </Panel>
        </div>
      </div>
    </div>
  );
}
