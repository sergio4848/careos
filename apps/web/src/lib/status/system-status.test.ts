import { describe, expect, it } from "vitest";

import type { ApiHealth } from "../api/health";
import { reconciliationInterval } from "../api/queries";
import type { ConnectionSnapshot } from "../realtime/connection";
import { describeFreshness, systemNotice } from "./system-status";

const NOW = Date.parse("2026-09-17T10:00:00Z");
const apiOk: ApiHealth = { status: "ok", since: null };
const apiDown: ApiHealth = { status: "unavailable", since: NOW - 5_000 };
const live: ConnectionSnapshot = { status: "live", attempt: 0, nextRetryAt: null };

describe("systemNotice", () => {
  it("is silent when everything is healthy or still starting", () => {
    expect(systemNotice(apiOk, live, NOW)).toBeNull();
    expect(systemNotice(apiOk, { status: "connecting", attempt: 0, nextRetryAt: null }, NOW)).toBeNull();
    expect(systemNotice(apiOk, { status: "unauthorised", attempt: 0, nextRetryAt: null }, NOW)).toBeNull();
  });

  it("puts an unreachable API above everything else", () => {
    const notice = systemNotice(apiDown, { status: "disconnected", attempt: 3, nextRetryAt: NOW + 4_000 }, NOW);
    expect(notice).toMatchObject({ severity: "critical", title: "CareOS server unreachable", canRetry: false });
    expect(notice?.detail).toMatch(/may be out of date/);
  });

  it("explains a lost live connection with a countdown and a retry", () => {
    const notice = systemNotice(apiOk, { status: "disconnected", attempt: 2, nextRetryAt: NOW + 4_200 }, NOW);
    expect(notice).toMatchObject({ severity: "warning", title: "Live updates interrupted", canRetry: true });
    expect(notice?.detail).toContain("every 10 seconds");
    expect(notice?.detail).toContain("Reconnecting in 5 s.");
  });

  it("reports reconnecting without offering a duplicate retry", () => {
    expect(systemNotice(apiOk, { status: "reconnecting", attempt: 2, nextRetryAt: null }, NOW)).toMatchObject({
      severity: "warning",
      canRetry: false,
    });
  });
});

describe("describeFreshness", () => {
  it("labels fresh and stale data", () => {
    expect(describeFreshness(0, NOW)).toEqual({ stale: false, label: "Loading…" });
    expect(describeFreshness(NOW - 3_000, NOW)).toEqual({ stale: false, label: "Updated just now" });
    expect(describeFreshness(NOW - 12_000, NOW)).toEqual({ stale: false, label: "Updated 12 s ago" });
    expect(describeFreshness(NOW - 45_000, NOW)).toEqual({
      stale: true,
      label: "Data may be out of date · last updated 45 s ago",
    });
    expect(describeFreshness(NOW - 185_000, NOW).label).toBe("Data may be out of date · last updated 3 min ago");
  });
});

describe("reconciliationInterval", () => {
  it("always reconciles with the API, faster when live updates are down", () => {
    expect(reconciliationInterval("live")).toBe(30_000);
    for (const status of ["connecting", "disconnected", "reconnecting", "unauthorised"]) {
      expect(reconciliationInterval(status)).toBe(10_000);
    }
  });
});
