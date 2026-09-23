"use client";

import type {
  AuditLogView,
  DashboardSummary,
  DeviceView,
  IncidentDetail,
  IncidentEventView,
  IncidentSummary,
  ResolutionCategory,
  ServiceUserProfile,
  SimulatorEventRequest,
  SimulatorEventResult,
  VoiceCallView,
} from "@careos/contracts";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useApi } from "../providers";
import { useRealtimeStatus } from "../realtime/RealtimeProvider";

export type IncidentScope = "active" | "awaiting_closure" | "recent";

export const queryKeys = {
  session: ["session"] as const,
  dashboard: ["dashboard"] as const,
  incidents: (scope: IncidentScope) => ["incidents", scope] as const,
  incident: (id: string) => ["incident", id] as const,
  timeline: (id: string) => ["incident", id, "timeline"] as const,
  calls: (id: string) => ["incident", id, "calls"] as const,
  devices: ["devices"] as const,
  serviceUser: (id: string) => ["service-user", id] as const,
  audit: (resourceId?: string) => ["audit", resourceId ?? "all"] as const,
};

/** Reconcile with the API even while live (a notification can be lost); poll faster when not. */
export const RECONCILE_LIVE_MS = 30_000;
export const RECONCILE_DEGRADED_MS = 10_000;

export function reconciliationInterval(status: string): number {
  return status === "live" ? RECONCILE_LIVE_MS : RECONCILE_DEGRADED_MS;
}

function usePollingFallback(): number {
  const { status } = useRealtimeStatus();
  return reconciliationInterval(status);
}

export function useDashboardSummary() {
  const api = useApi();
  const refetchInterval = usePollingFallback();
  return useQuery({
    queryKey: queryKeys.dashboard,
    queryFn: ({ signal }) => api.get<DashboardSummary>("/v1/dashboard/summary", signal),
    refetchInterval,
  });
}

export function useIncidents(scope: IncidentScope) {
  const api = useApi();
  const refetchInterval = usePollingFallback();
  return useQuery({
    queryKey: queryKeys.incidents(scope),
    queryFn: ({ signal }) => api.get<IncidentSummary[]>(`/v1/incidents?scope=${scope}&limit=100`, signal),
    refetchInterval,
  });
}

export function useIncident(id: string) {
  const api = useApi();
  const refetchInterval = usePollingFallback();
  return useQuery({
    queryKey: queryKeys.incident(id),
    queryFn: ({ signal }) => api.get<IncidentDetail>(`/v1/incidents/${id}`, signal),
    refetchInterval,
    staleTime: 30_000,
  });
}

export function useTimeline(id: string) {
  const api = useApi();
  const refetchInterval = usePollingFallback();
  return useQuery({
    queryKey: queryKeys.timeline(id),
    queryFn: ({ signal }) => api.get<IncidentEventView[]>(`/v1/incidents/${id}/timeline`, signal),
    refetchInterval,
  });
}

export function useIncidentCalls(id: string) {
  const api = useApi();
  const refetchInterval = usePollingFallback();
  return useQuery({
    queryKey: queryKeys.calls(id),
    queryFn: ({ signal }) => api.get<VoiceCallView[]>(`/v1/incidents/${id}/calls`, signal),
    refetchInterval,
  });
}

export function useDevices() {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.devices,
    queryFn: ({ signal }) => api.get<DeviceView[]>("/v1/devices", signal),
  });
}

export function useServiceUser(id: string) {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.serviceUser(id),
    queryFn: ({ signal }) => api.get<ServiceUserProfile>(`/v1/service-users/${id}`, signal),
    staleTime: 30_000,
  });
}

export function useAuditLogs(resourceId?: string, enabled = true) {
  const api = useApi();
  const query = resourceId ? `?resource_id=${encodeURIComponent(resourceId)}&limit=100` : "?limit=200";
  return useQuery({
    queryKey: queryKeys.audit(resourceId),
    queryFn: ({ signal }) => api.get<AuditLogView[]>(`/v1/audit-logs${query}`, signal),
    enabled,
  });
}

function useIncidentMutation<TVariables>(
  request: (variables: TVariables) => Promise<IncidentDetail>,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: request,
    onSuccess: (detail) => {
      queryClient.setQueryData(queryKeys.incident(detail.id), detail);
      void queryClient.invalidateQueries({ queryKey: ["incidents"] });
      void queryClient.invalidateQueries({ queryKey: queryKeys.timeline(detail.id) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.dashboard });
      void queryClient.invalidateQueries({ queryKey: ["audit"] });
    },
    onError: () => {
      // Someone else may have acted first: refresh everything the operator is looking at.
      void queryClient.invalidateQueries({ queryKey: ["incidents"] });
      void queryClient.invalidateQueries({ queryKey: ["incident"] });
    },
  });
}

export function useTakeOver() {
  const api = useApi();
  return useIncidentMutation((incidentId: string) =>
    api.post<IncidentDetail>(`/v1/incidents/${incidentId}/takeover`),
  );
}

export function useResolve() {
  const api = useApi();
  return useIncidentMutation(
    ({ incidentId, category, notes }: { incidentId: string; category: ResolutionCategory; notes?: string }) =>
      api.post<IncidentDetail>(`/v1/incidents/${incidentId}/resolve`, { category, notes: notes || null }),
  );
}

export function useCloseIncident() {
  const api = useApi();
  return useIncidentMutation(({ incidentId, notes }: { incidentId: string; notes?: string }) =>
    api.post<IncidentDetail>(`/v1/incidents/${incidentId}/close`, { notes: notes || null }),
  );
}

export function useStopCall() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ incidentId, callId }: { incidentId: string; callId: string }) =>
      api.post<void>(`/v1/incidents/${incidentId}/calls/${callId}/stop`),
    onSettled: (_data, _error, { incidentId }) => {
      // Success or conflict, the truth lives in PostgreSQL: refetch it.
      void queryClient.invalidateQueries({ queryKey: queryKeys.incident(incidentId) });
      void queryClient.invalidateQueries({ queryKey: ["audit"] });
    },
  });
}

export function useEscalateNow() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ incidentId }: { incidentId: string }) =>
      api.post<{ accelerated_steps: number }>(`/v1/incidents/${incidentId}/escalate-now`),
    onSettled: (_data, _error, { incidentId }) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.incident(incidentId) });
      void queryClient.invalidateQueries({ queryKey: ["audit"] });
    },
  });
}

export function useSimulateEvent() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ deviceId, request }: { deviceId: string; request: SimulatorEventRequest }) =>
      api.post<SimulatorEventResult>(`/v1/simulator/devices/${deviceId}/events`, request),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: queryKeys.devices }),
  });
}
