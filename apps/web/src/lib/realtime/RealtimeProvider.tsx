"use client";

import { useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";

import { useConfig } from "../config";
import { RealtimeConnection, type ConnectionSnapshot, type ConnectionStatus } from "./connection";
import { isRealtimeMessage, parseSocketMessage, queryKeysToInvalidate } from "./messages";

export type RealtimeStatus = ConnectionStatus;

interface RealtimeState extends ConnectionSnapshot {
  /** Latest screen-reader announcement for new alarms. */
  announcement: string;
  /** Reconnect now instead of waiting for the backoff timer. */
  retryNow: () => void;
}

const INITIAL: ConnectionSnapshot = { status: "connecting", attempt: 0, nextRetryAt: null };

const RealtimeContext = createContext<RealtimeState>({ ...INITIAL, announcement: "", retryNow: () => {} });

export function useRealtimeStatus(): RealtimeState {
  return useContext(RealtimeContext);
}

export function RealtimeProvider({ children, onUnauthorised }: { children: ReactNode; onUnauthorised?: () => void }) {
  const { wsUrl } = useConfig();
  const queryClient = useQueryClient();
  const [snapshot, setSnapshot] = useState<ConnectionSnapshot>(INITIAL);
  const [announcement, setAnnouncement] = useState("");
  const connectionRef = useRef<RealtimeConnection | null>(null);
  const unauthorisedRef = useRef(onUnauthorised);

  useEffect(() => {
    unauthorisedRef.current = onUnauthorised;
  }, [onUnauthorised]);

  useEffect(() => {
    const connection = new RealtimeConnection({
      url: wsUrl,
      onSnapshot: setSnapshot,
      // The socket is a notification channel: on every (re)connect, refetch from the API.
      onOpen: () => void queryClient.invalidateQueries(),
      onUnauthorised: () => unauthorisedRef.current?.(),
      onMessage: (data) => {
        const message = parseSocketMessage(data);
        if (!message || !isRealtimeMessage(message)) return;
        for (const queryKey of queryKeysToInvalidate(message)) {
          void queryClient.invalidateQueries({ queryKey });
        }
        if (message.type === "incident.created") {
          const priority = message.payload.priority ?? "NEW";
          setAnnouncement(`New ${priority.toLowerCase()} incident ${message.payload.reference ?? ""} received`);
        }
      },
    });
    connectionRef.current = connection;
    connection.start();
    const onOnline = () => connection.retryNow();
    window.addEventListener("online", onOnline);
    return () => {
      window.removeEventListener("online", onOnline);
      connection.stop();
      connectionRef.current = null;
    };
  }, [wsUrl, queryClient]);

  const value: RealtimeState = {
    ...snapshot,
    announcement,
    retryNow: () => connectionRef.current?.retryNow(),
  };
  return <RealtimeContext.Provider value={value}>{children}</RealtimeContext.Provider>;
}
