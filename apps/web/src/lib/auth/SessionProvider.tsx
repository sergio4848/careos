"use client";

import type { SessionView } from "@careos/contracts";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { usePathname, useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useMemo, type ReactNode } from "react";

import { isApiError } from "../api/client";
import { queryKeys } from "../api/queries";
import { useApi } from "../providers";

export type Permission = SessionView["permissions"][number];

interface SessionContextValue {
  session: SessionView;
  can: (permission: string) => boolean;
  logout: () => Promise<void>;
  expire: () => void;
}

const SessionContext = createContext<SessionContextValue | null>(null);

export function useSession(): SessionContextValue {
  const value = useContext(SessionContext);
  if (!value) throw new Error("useSession must be used inside <SessionProvider>");
  return value;
}

export function SessionProvider({ children, fallback }: { children: ReactNode; fallback: ReactNode }) {
  const api = useApi();
  const router = useRouter();
  const pathname = usePathname();
  const queryClient = useQueryClient();

  const { data, error } = useQuery({
    queryKey: queryKeys.session,
    queryFn: async ({ signal }) => {
      const session = await api.get<SessionView>("/v1/auth/me", signal);
      api.setCsrfToken(session.csrf_token);
      return session;
    },
    staleTime: 60_000,
    refetchInterval: 5 * 60_000,
  });

  const expire = useCallback(() => {
    api.setCsrfToken(null);
    queryClient.clear();
    router.replace(`/login?next=${encodeURIComponent(pathname)}`);
  }, [api, pathname, queryClient, router]);

  useEffect(() => {
    if (isApiError(error, 401)) expire();
  }, [error, expire]);

  const logout = useCallback(async () => {
    try {
      await api.post("/v1/auth/logout");
    } finally {
      api.setCsrfToken(null);
      queryClient.clear();
      router.replace("/login");
    }
  }, [api, queryClient, router]);

  const value = useMemo<SessionContextValue | null>(
    () =>
      data
        ? {
            session: data,
            can: (permission: string) => data.permissions.includes(permission),
            logout,
            expire,
          }
        : null,
    [data, expire, logout],
  );

  if (!value) return <>{fallback}</>;
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}
