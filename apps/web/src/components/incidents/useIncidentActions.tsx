"use client";

import { ACTIVE_STATUSES, type IncidentSummary } from "@careos/contracts";
import { useCallback, useState, type ReactNode } from "react";

import { isApiError } from "@/lib/api/client";
import { useResolve, useTakeOver } from "@/lib/api/queries";
import { useSession } from "@/lib/auth/SessionProvider";

import { ResolveDialog } from "./ResolveDialog";

type ActionTarget = Pick<IncidentSummary, "id" | "status" | "assignee" | "is_active" | "service_user" | "reference">;

const NOT_TAKEOVERABLE = new Set(["RECEIVED", "VALIDATING"]);

export function describeActionError(error: unknown): string {
  if (isApiError(error, 409)) return `${error.message} The board has been refreshed.`;
  if (isApiError(error)) return error.message;
  return "The action could not be completed. Please retry.";
}

/** Shared take-over / resolve behaviour for the live board and the incident page. */
export function useIncidentActions() {
  const { session, can } = useSession();
  const takeOver = useTakeOver();
  const resolve = useResolve();
  const [resolving, setResolving] = useState<ActionTarget | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const canTakeOver = useCallback(
    (incident: ActionTarget) =>
      can("incidents:takeover") &&
      incident.is_active &&
      !incident.assignee &&
      ACTIVE_STATUSES.includes(incident.status) &&
      !NOT_TAKEOVERABLE.has(incident.status),
    [can],
  );

  const canResolve = useCallback(
    (incident: ActionTarget) =>
      can("incidents:resolve") &&
      incident.is_active &&
      (!incident.assignee ||
        incident.assignee.id === session.user.id ||
        can("incidents:override_assignment")),
    [can, session.user.id],
  );

  const startTakeOver = useCallback(
    (incident: ActionTarget) => {
      setNotice(null);
      takeOver.mutate(incident.id, { onError: (error) => setNotice(describeActionError(error)) });
    },
    [takeOver],
  );

  const dialog: ReactNode = resolving ? (
    <ResolveDialog
      subject={resolving.service_user?.display_name ?? resolving.reference}
      submitting={resolve.isPending}
      error={resolve.error ? describeActionError(resolve.error) : null}
      onCancel={() => {
        resolve.reset();
        setResolving(null);
      }}
      onSubmit={({ category, notes }) =>
        resolve.mutate(
          { incidentId: resolving.id, category, notes },
          {
            onSuccess: () => {
              setResolving(null);
              setNotice(null);
            },
          },
        )
      }
    />
  ) : null;

  return {
    canTakeOver,
    canResolve,
    startTakeOver,
    takingOverId: takeOver.isPending ? takeOver.variables : undefined,
    openResolve: (incident: ActionTarget) => {
      resolve.reset();
      setResolving(incident);
    },
    notice,
    clearNotice: () => setNotice(null),
    dialog,
  };
}
