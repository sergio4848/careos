import { describe, expect, it, vi } from "vitest";

import { createApiClient } from "./client";
import { createApiHealthStore } from "./health";

function respond(status: number) {
  return (async () =>
    new Response(JSON.stringify(status >= 400 ? { error: { code: "x", message: "x" } } : {}), {
      status,
      headers: { "content-type": "application/json" },
    })) as unknown as typeof fetch;
}

describe("API health", () => {
  it("notifies listeners only when availability changes", () => {
    let clock = 1_000;
    const store = createApiHealthStore(() => clock);
    const listener = vi.fn();
    store.subscribe(listener);

    store.reportSuccess();
    expect(listener).not.toHaveBeenCalled();
    store.reportFailure();
    store.reportFailure();
    expect(listener).toHaveBeenCalledTimes(1);
    expect(store.getSnapshot()).toEqual({ status: "unavailable", since: 1_000 });
    clock = 5_000;
    store.reportSuccess();
    expect(store.getSnapshot()).toEqual({ status: "ok", since: 5_000 });
  });

  it("treats network failures and gateway errors as unavailable, API answers as available", async () => {
    const store = createApiHealthStore();
    const offline = createApiClient(
      "http://api.test",
      (async () => {
        throw new TypeError("Failed to fetch");
      }) as unknown as typeof fetch,
      store,
    );
    await offline.get("/v1/incidents").catch(() => undefined);
    expect(store.getSnapshot().status).toBe("unavailable");

    await createApiClient("http://api.test", respond(409), store)
      .post("/v1/incidents/1/takeover")
      .catch(() => undefined);
    expect(store.getSnapshot().status).toBe("ok"); // a 409 is the API answering

    await createApiClient("http://api.test", respond(503), store).get("/v1/incidents").catch(() => undefined);
    expect(store.getSnapshot().status).toBe("unavailable");

    await createApiClient("http://api.test", respond(200), store).get("/v1/incidents");
    expect(store.getSnapshot().status).toBe("ok");
  });
});
