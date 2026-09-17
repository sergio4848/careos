/**
 * CareOS shared contracts for TypeScript consumers.
 *
 * Source of truth: the backend's Pydantic models. Regenerate with:
 *   python -m careos.cli export-contracts   (apps/backend)
 *   npm run contracts:generate             (repository root)
 */
import type { components } from "./generated/api";

type Schemas = components["schemas"];

export type IncidentStatus = Schemas["IncidentStatus"];
export type IncidentPriority = Schemas["IncidentPriority"];
export type ResolutionCategory = Schemas["ResolutionCategory"];
export type IncidentSummary = Schemas["IncidentSummary"];
export type IncidentDetail = Schemas["IncidentDetail"];
export type IncidentEventView = Schemas["IncidentEventView"];
export type ScheduledActionView = Schemas["ScheduledActionView"];
export type ResolveIncidentRequest = Schemas["ResolveIncidentRequest"];
export type DashboardSummary = Schemas["DashboardSummary"];
export type DeviceView = Schemas["DeviceView"];
export type ServiceUserProfile = Schemas["ServiceUserProfile"];
export type ServiceUserSummary = Schemas["ServiceUserSummary"];
export type TrustedContactView = Schemas["TrustedContactView"];
export type SessionView = Schemas["SessionView"];
export type AuditLogView = Schemas["AuditLogView"];
export type SimulatorEventRequest = Schemas["SimulatorEventRequest"];
export type SimulatorEventResult = Schemas["SimulatorEventResult"];

/** Realtime WebSocket message (packages/contracts/schemas/careos-realtime-message.v1.schema.json). */
export type RealtimeMessageType = "incident.created" | "incident.updated" | "device.updated";

export interface RealtimeMessage {
  id: string;
  type: RealtimeMessageType;
  organisation_id: string;
  occurred_at: string;
  incident_id: string | null;
  device_id: string | null;
  payload: {
    status?: IncidentStatus;
    priority?: IncidentPriority;
    reference?: string;
    event_type?: string | null;
  };
}

export type ControlMessage = { type: "connection.ready" } | { type: "heartbeat" };

export const ACTIVE_STATUSES: readonly IncidentStatus[] = [
  "RECEIVED",
  "VALIDATING",
  "OPEN",
  "CONTACTING",
  "ACKNOWLEDGED",
  "IN_PROGRESS",
  "ESCALATED",
  "FAILED",
  "DEVICE_ERROR",
];

export const RESOLUTION_CATEGORIES: readonly ResolutionCategory[] = [
  "USER_SAFE",
  "CAREGIVER_RESPONDED",
  "FAMILY_RESPONDED",
  "FALSE_ALARM",
  "EMERGENCY_SERVICES",
  "DEVICE_ERROR",
  "OTHER",
];

export type { components, paths } from "./generated/api";
