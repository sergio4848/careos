import type { RealtimeMessage } from "@careos/contracts";
import { describe, expect, it } from "vitest";

import { isRealtimeMessage, parseSocketMessage, queryKeysToInvalidate, reconnectDelay } from "./messages";

const base: RealtimeMessage = {
  id: "m1",
  type: "incident.created",
  organisation_id: "org-1",
  occurred_at: "2026-09-16T10:00:00Z",
  incident_id: "inc-1",
  device_id: null,
  payload: { status: "OPEN", priority: "CRITICAL", reference: "INC-1" },
};

describe("queryKeysToInvalidate", () => {
  it("refreshes the board and summary when an incident is created", () => {
    expect(queryKeysToInvalidate(base)).toEqual([["incidents"], ["dashboard"]]);
  });

  it("refreshes the specific incident (detail, timeline, audit) when it changes", () => {
    expect(queryKeysToInvalidate({ ...base, type: "incident.updated" })).toEqual([
      ["incidents"],
      ["dashboard"],
      ["incident", "inc-1"],
      ["audit"],
    ]);
  });

  it("refreshes devices on telemetry", () => {
    expect(queryKeysToInvalidate({ ...base, type: "device.updated", incident_id: null })).toEqual([
      ["devices"],
      ["dashboard"],
    ]);
  });
});

describe("parseSocketMessage", () => {
  it("ignores pong frames and malformed data", () => {
    expect(parseSocketMessage("pong")).toBeNull();
    expect(parseSocketMessage("{not json")).toBeNull();
    expect(parseSocketMessage('{"no":"type"}')).toBeNull();
  });

  it("distinguishes control frames from realtime messages", () => {
    const ready = parseSocketMessage('{"type":"connection.ready"}');
    const message = parseSocketMessage(JSON.stringify(base));
    expect(ready && isRealtimeMessage(ready)).toBe(false);
    expect(message && isRealtimeMessage(message)).toBe(true);
  });
});

describe("reconnectDelay", () => {
  it("backs off exponentially with a 30s cap", () => {
    const noJitter = () => 0.5;
    expect(reconnectDelay(0, noJitter)).toBe(1_000);
    expect(reconnectDelay(3, noJitter)).toBe(8_000);
    expect(reconnectDelay(10, noJitter)).toBe(30_000);
  });
});
