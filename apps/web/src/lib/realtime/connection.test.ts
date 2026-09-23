import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RealtimeConnection, type ConnectionSnapshot, type SocketLike } from "./connection";

class FakeSocket implements SocketLike {
  static instances: FakeSocket[] = [];
  readyState = 0;
  sent: string[] = [];
  closedWith: number | undefined;
  onopen: SocketLike["onopen"] = null;
  onmessage: SocketLike["onmessage"] = null;
  onclose: SocketLike["onclose"] = null;
  onerror: SocketLike["onerror"] = null;

  constructor(readonly url: string) {
    FakeSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(data);
  }

  close(code = 1000) {
    this.closedWith = code;
    this.readyState = 3;
  }

  open() {
    this.readyState = 1;
    this.onopen?.(new Event("open"));
  }

  message(data: string) {
    this.onmessage?.({ data } as MessageEvent<string>);
  }

  drop(code = 1006) {
    this.readyState = 3;
    this.onclose?.({ code } as CloseEvent);
  }
}

const latest = () => FakeSocket.instances[FakeSocket.instances.length - 1]!;

function setup() {
  const snapshots: ConnectionSnapshot[] = [];
  const onOpen = vi.fn();
  const onMessage = vi.fn();
  const onUnauthorised = vi.fn();
  const connection = new RealtimeConnection({
    url: "ws://api.test/v1/ws",
    onSnapshot: (snapshot) => snapshots.push(snapshot),
    onOpen,
    onMessage,
    onUnauthorised,
    createSocket: (url) => new FakeSocket(url),
    random: () => 0.5, // jitter factor exactly 1.0
    pingIntervalMs: 20_000,
    staleAfterMs: 60_000,
  });
  connection.start();
  return { connection, snapshots, onOpen, onMessage, onUnauthorised, statuses: () => snapshots.map((s) => s.status) };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-09-17T10:00:00Z"));
  FakeSocket.instances = [];
});

afterEach(() => {
  vi.useRealTimers();
});

describe("RealtimeConnection", () => {
  it("goes live on open and refetches from the REST API", () => {
    const { statuses, onOpen, onMessage } = setup();
    latest().open();
    latest().message('{"type":"incident.created"}');
    expect(statuses()).toEqual(["connecting", "live"]);
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(onMessage).toHaveBeenCalledWith('{"type":"incident.created"}');
  });

  it("recovers from a dropped connection and refetches again after reconnecting", () => {
    const { connection, statuses, onOpen } = setup();
    latest().open();
    latest().drop(1006);

    expect(connection.current).toEqual({
      status: "disconnected",
      attempt: 1,
      nextRetryAt: Date.now() + 1_000,
    });
    vi.advanceTimersByTime(999);
    expect(FakeSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeSocket.instances).toHaveLength(2);
    expect(connection.current.status).toBe("reconnecting");

    latest().open();
    expect(statuses()).toEqual(["connecting", "live", "disconnected", "reconnecting", "live"]);
    expect(onOpen).toHaveBeenCalledTimes(2); // WebSocket is a notification channel, not the truth
  });

  it("backs off exponentially and never waits longer than 30 seconds", () => {
    const { connection } = setup();
    const delays: number[] = [];
    for (let attempt = 0; attempt < 8; attempt += 1) {
      latest().drop(1006);
      const { nextRetryAt } = connection.current;
      delays.push((nextRetryAt ?? 0) - Date.now());
      vi.advanceTimersByTime(nextRetryAt! - Date.now());
    }
    expect(delays).toEqual([1_000, 2_000, 4_000, 8_000, 16_000, 30_000, 30_000, 30_000]);
  });

  it("stops retrying when the session is no longer valid", () => {
    const { connection, onUnauthorised } = setup();
    latest().drop(4401);
    vi.advanceTimersByTime(120_000);
    expect(connection.current.status).toBe("unauthorised");
    expect(FakeSocket.instances).toHaveLength(1);
    expect(onUnauthorised).toHaveBeenCalledTimes(1);
  });

  it("detects a silently dead connection and reconnects", () => {
    const { connection } = setup();
    const first = latest();
    first.open();
    vi.advanceTimersByTime(40_000);
    first.message("pong"); // activity keeps it alive
    vi.advanceTimersByTime(60_000);
    expect(connection.current.status).toBe("live");

    vi.advanceTimersByTime(20_000); // 80 s of silence
    expect(first.closedWith).toBe(4000);
    expect(connection.current.status).toBe("disconnected");
    vi.advanceTimersByTime(1_000);
    expect(FakeSocket.instances).toHaveLength(2);
  });

  it("pings the server while live", () => {
    setup();
    latest().open();
    vi.advanceTimersByTime(40_000);
    expect(latest().sent).toEqual(["ping", "ping"]);
  });

  it("reconnects immediately on retryNow instead of waiting for the backoff", () => {
    const { connection } = setup();
    for (let i = 0; i < 5; i += 1) {
      latest().drop(1006);
      vi.advanceTimersByTime(connection.current.nextRetryAt! - Date.now());
    }
    latest().drop(1006); // next automatic retry is 30 s away
    connection.retryNow();
    expect(FakeSocket.instances).toHaveLength(7);
    expect(connection.current.status).toBe("reconnecting");
    vi.advanceTimersByTime(30_000);
    expect(FakeSocket.instances).toHaveLength(7); // the cancelled timer did not fire a duplicate
  });

  it("does not reconnect after stop", () => {
    const { connection } = setup();
    const socket = latest();
    socket.open();
    connection.stop();
    expect(socket.closedWith).toBe(1000);
    vi.advanceTimersByTime(120_000);
    expect(FakeSocket.instances).toHaveLength(1);
  });
});
