"use client";

import { useSyncExternalStore } from "react";

/**
 * Whether the CareOS API is reachable, as observed by the console's real requests (no extra
 * polling). A network failure or a 502/503/504 marks it unavailable; any other response marks
 * it available again. Listeners are notified only when the status changes.
 */
export interface ApiHealth {
  status: "ok" | "unavailable";
  /** Epoch ms of the last status change, null before the first change. */
  since: number | null;
}

export interface ApiHealthReporter {
  reportSuccess(): void;
  reportFailure(): void;
}

export interface ApiHealthStore extends ApiHealthReporter {
  subscribe(listener: () => void): () => void;
  getSnapshot(): ApiHealth;
}

const INITIAL: ApiHealth = { status: "ok", since: null };

/** Gateway/proxy statuses that mean "the API is not there", as opposed to an API answer. */
export const UNAVAILABLE_STATUSES: ReadonlySet<number> = new Set([502, 503, 504]);

export function createApiHealthStore(now: () => number = Date.now): ApiHealthStore {
  let snapshot = INITIAL;
  const listeners = new Set<() => void>();
  const transition = (status: ApiHealth["status"]) => {
    if (snapshot.status === status) return;
    snapshot = { status, since: now() };
    listeners.forEach((listener) => listener());
  };
  return {
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    getSnapshot: () => snapshot,
    reportSuccess: () => transition("ok"),
    reportFailure: () => transition("unavailable"),
  };
}

export function useApiHealthSnapshot(store: ApiHealthStore): ApiHealth {
  return useSyncExternalStore(store.subscribe, store.getSnapshot, () => INITIAL);
}
