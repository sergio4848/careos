import { afterEach, describe, expect, it, vi } from "vitest";

import { resetServerClock, serverNow } from "../time/server-clock";
import { ApiError, createApiClient } from "./client";

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}) {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

afterEach(() => resetServerClock());

describe("createApiClient", () => {
  it("sends credentials and attaches the CSRF token only to state-changing requests", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(200, { ok: true }));
    const api = createApiClient("http://api.test", fetchMock as unknown as typeof fetch);
    api.setCsrfToken("csrf-123");

    await api.get("/v1/incidents");
    await api.post("/v1/incidents/1/takeover");

    const [, getInit] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    const [, postInit] = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
    expect(getInit.credentials).toBe("include");
    expect((getInit.headers as Record<string, string>)["X-CSRF-Token"]).toBeUndefined();
    expect((postInit.headers as Record<string, string>)["X-CSRF-Token"]).toBe("csrf-123");
  });

  it("raises ApiError with the server's error envelope", async () => {
    const api = createApiClient(
      "http://api.test",
      (async () =>
        jsonResponse(409, {
          error: { code: "incident_already_assigned", message: "Already taken", request_id: "req-1", details: {} },
        })) as unknown as typeof fetch,
    );
    const error = await api.post("/v1/incidents/1/takeover").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 409, code: "incident_already_assigned", requestId: "req-1" });
  });

  it("maps network failures to a network_error", async () => {
    const api = createApiClient("http://api.test", (async () => {
      throw new TypeError("Failed to fetch");
    }) as unknown as typeof fetch);
    await expect(api.get("/health")).rejects.toMatchObject({ status: 0, code: "network_error" });
  });

  it("corrects the operator clock using the server Date header", async () => {
    const serverTime = new Date(Date.now() + 5 * 60_000);
    const api = createApiClient(
      "http://api.test",
      (async () => jsonResponse(200, {}, { date: serverTime.toUTCString() })) as unknown as typeof fetch,
    );
    await api.get("/health");
    expect(Math.abs(serverNow() - serverTime.getTime())).toBeLessThan(2_000);
  });
});
