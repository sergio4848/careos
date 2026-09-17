import { reconnectDelay } from "./messages";

/**
 * Operator WebSocket lifecycle, independent of React so every failure path is unit tested.
 *
 * The socket is a notification channel, never the source of truth: whenever it (re)opens we
 * ask the caller to refetch from the REST API, because anything may have changed while it
 * was down.
 *
 *   connecting ──open──▶ live ──close/stale──▶ disconnected ──backoff timer──▶ reconnecting
 *        ▲                                          │                               │
 *        └────────────── retryNow() ────────────────┘◀──────── close ───────────────┘
 *   4401/4403 close ─▶ unauthorised (terminal: the session is gone, retrying cannot help)
 */
export type ConnectionStatus = "connecting" | "live" | "disconnected" | "reconnecting" | "unauthorised";

export interface ConnectionSnapshot {
  status: ConnectionStatus;
  /** Consecutive failed attempts since the last successful open. */
  attempt: number;
  /** Epoch ms of the next automatic retry while disconnected, otherwise null. */
  nextRetryAt: number | null;
}

export interface SocketLike {
  readonly readyState: number;
  onopen: ((event: Event) => void) | null;
  onmessage: ((event: MessageEvent<string>) => void) | null;
  onclose: ((event: CloseEvent) => void) | null;
  onerror: ((event: Event) => void) | null;
  send(data: string): void;
  close(code?: number, reason?: string): void;
}

export interface ConnectionOptions {
  url: string;
  onSnapshot: (snapshot: ConnectionSnapshot) => void;
  onMessage: (data: string) => void;
  /** Called on every successful open: refetch authoritative state from the REST API. */
  onOpen: () => void;
  onUnauthorised?: () => void;
  createSocket?: (url: string) => SocketLike;
  now?: () => number;
  random?: () => number;
  pingIntervalMs?: number;
  /** Close and reconnect when nothing (message, heartbeat, pong) arrives for this long. */
  staleAfterMs?: number;
}

const OPEN = 1;
export const UNAUTHORISED_CLOSE_CODES = new Set([4401, 4403]);

export class RealtimeConnection {
  private socket: SocketLike | null = null;
  private attempt = 0;
  private everOpened = false;
  private stopped = false;
  private retryTimer: ReturnType<typeof setTimeout> | undefined;
  private pingTimer: ReturnType<typeof setInterval> | undefined;
  private watchdogTimer: ReturnType<typeof setInterval> | undefined;
  private lastActivity = 0;
  private snapshot: ConnectionSnapshot = { status: "connecting", attempt: 0, nextRetryAt: null };

  private readonly createSocket: (url: string) => SocketLike;
  private readonly now: () => number;
  private readonly random: () => number;
  private readonly pingIntervalMs: number;
  private readonly staleAfterMs: number;

  constructor(private readonly options: ConnectionOptions) {
    this.createSocket = options.createSocket ?? ((url) => new WebSocket(url) as SocketLike);
    this.now = options.now ?? Date.now;
    this.random = options.random ?? Math.random;
    this.pingIntervalMs = options.pingIntervalMs ?? 20_000;
    this.staleAfterMs = options.staleAfterMs ?? 60_000;
  }

  get current(): ConnectionSnapshot {
    return this.snapshot;
  }

  start(): void {
    this.stopped = false;
    this.connect();
  }

  /** Reconnect immediately (browser came back online, or the operator pressed "Retry"). */
  retryNow(): void {
    if (this.stopped || this.snapshot.status === "live" || this.snapshot.status === "unauthorised") return;
    if (this.snapshot.status === "reconnecting" || this.snapshot.status === "connecting") return;
    clearTimeout(this.retryTimer);
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    clearTimeout(this.retryTimer);
    this.clearLiveTimers();
    const socket = this.socket;
    this.socket = null;
    if (socket) {
      socket.onclose = null;
      socket.close(1000, "navigation");
    }
  }

  private publish(snapshot: ConnectionSnapshot): void {
    this.snapshot = snapshot;
    this.options.onSnapshot(snapshot);
  }

  private connect(): void {
    this.publish({
      status: this.everOpened || this.attempt > 0 ? "reconnecting" : "connecting",
      attempt: this.attempt,
      nextRetryAt: null,
    });
    let socket: SocketLike;
    try {
      socket = this.createSocket(this.options.url);
    } catch {
      this.scheduleRetry();
      return;
    }
    this.socket = socket;

    socket.onopen = () => {
      if (socket !== this.socket) return;
      this.attempt = 0;
      this.everOpened = true;
      this.lastActivity = this.now();
      this.publish({ status: "live", attempt: 0, nextRetryAt: null });
      this.startLiveTimers(socket);
      this.options.onOpen();
    };
    socket.onmessage = (event) => {
      if (socket !== this.socket) return;
      this.lastActivity = this.now();
      this.options.onMessage(event.data);
    };
    socket.onerror = () => {
      // The browser always follows an error with a close event; recovery happens there.
    };
    socket.onclose = (event) => {
      if (socket !== this.socket) return;
      this.socket = null;
      this.clearLiveTimers();
      if (this.stopped) return;
      if (UNAUTHORISED_CLOSE_CODES.has(event.code)) {
        this.publish({ status: "unauthorised", attempt: this.attempt, nextRetryAt: null });
        this.options.onUnauthorised?.();
        return;
      }
      this.scheduleRetry();
    };
  }

  private scheduleRetry(): void {
    const delay = reconnectDelay(this.attempt, this.random);
    this.attempt += 1;
    this.publish({ status: "disconnected", attempt: this.attempt, nextRetryAt: this.now() + delay });
    this.retryTimer = setTimeout(() => this.connect(), delay);
  }

  private startLiveTimers(socket: SocketLike): void {
    this.pingTimer = setInterval(() => {
      if (socket.readyState === OPEN) socket.send("ping");
    }, this.pingIntervalMs);
    this.watchdogTimer = setInterval(() => {
      if (this.now() - this.lastActivity > this.staleAfterMs) {
        // A half-open TCP connection never fires "close" by itself. Force the recovery path.
        this.socket = null;
        this.clearLiveTimers();
        socket.onclose = null;
        socket.close(4000, "stale");
        if (!this.stopped) this.scheduleRetry();
      }
    }, Math.min(this.pingIntervalMs, this.staleAfterMs));
  }

  private clearLiveTimers(): void {
    clearInterval(this.pingTimer);
    clearInterval(this.watchdogTimer);
  }
}
