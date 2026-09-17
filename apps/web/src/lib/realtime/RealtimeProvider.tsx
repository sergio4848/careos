"use client";

import { useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";

import { useConfig } from "../config";
import { isRealtimeMessage, parseSocketMessage, queryKeysToInvalidate, reconnectDelay } from "./messages";

export type RealtimeStatus = "connecting" | "live" | "reconnecting" | "unauthorised";

interface RealtimeState {
  status: RealtimeStatus;
  /** Latest screen-reader announcement for new alarms. */
  announcement: string;
}

const RealtimeContext = createContext<RealtimeState>({ status: "connecting", announcement: "" });

export function useRealtimeStatus(): RealtimeState {
  return useContext(RealtimeContext);
}

const PING_INTERVAL_MS = 20_000;

export function RealtimeProvider({ children, onUnauthorised }: { children: ReactNode; onUnauthorised?: () => void }) {
  const { wsUrl } = useConfig();
  const queryClient = useQueryClient();
  const [state, setState] = useState<RealtimeState>({ status: "connecting", announcement: "" });
  const unauthorisedRef = useRef(onUnauthorised);

  useEffect(() => {
    unauthorisedRef.current = onUnauthorised;
  }, [onUnauthorised]);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let attempt = 0;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    let pingTimer: ReturnType<typeof setInterval> | undefined;
    let disposed = false;

    const setStatus = (status: RealtimeStatus) => setState((prev) => ({ ...prev, status }));

    const connect = () => {
      setStatus(attempt === 0 ? "connecting" : "reconnecting");
      socket = new WebSocket(wsUrl);

      socket.onopen = () => {
        attempt = 0;
        setStatus("live");
        // Anything could have happened while disconnected: refetch what is on screen.
        void queryClient.invalidateQueries();
        pingTimer = setInterval(() => socket?.readyState === WebSocket.OPEN && socket.send("ping"), PING_INTERVAL_MS);
      };

      socket.onmessage = (event: MessageEvent<string>) => {
        const message = parseSocketMessage(event.data);
        if (!message || !isRealtimeMessage(message)) return;
        for (const queryKey of queryKeysToInvalidate(message)) {
          void queryClient.invalidateQueries({ queryKey });
        }
        if (message.type === "incident.created") {
          const priority = message.payload.priority ?? "NEW";
          setState((prev) => ({
            ...prev,
            announcement: `New ${priority.toLowerCase()} incident ${message.payload.reference ?? ""} received`,
          }));
        }
      };

      socket.onclose = (event) => {
        clearInterval(pingTimer);
        if (disposed) return;
        if (event.code === 4401 || event.code === 4403) {
          setStatus("unauthorised");
          unauthorisedRef.current?.();
          return;
        }
        setStatus("reconnecting");
        reconnectTimer = setTimeout(connect, reconnectDelay(attempt));
        attempt += 1;
      };
    };

    connect();
    return () => {
      disposed = true;
      clearTimeout(reconnectTimer);
      clearInterval(pingTimer);
      socket?.close(1000, "navigation");
    };
  }, [wsUrl, queryClient]);

  return <RealtimeContext.Provider value={state}>{children}</RealtimeContext.Provider>;
}
