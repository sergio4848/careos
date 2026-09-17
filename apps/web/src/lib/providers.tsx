"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createContext, useContext, useState, type ReactNode } from "react";

import { createApiClient, isApiError, type ApiClient } from "./api/client";
import { ConfigProvider, type RuntimeConfig } from "./config";

const ApiContext = createContext<ApiClient | null>(null);

export function useApi(): ApiClient {
  const api = useContext(ApiContext);
  if (!api) throw new Error("useApi must be used inside <Providers>");
  return api;
}

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 5_000,
        refetchOnWindowFocus: true,
        retry: (failureCount, error) =>
          failureCount < 2 && !(isApiError(error) && error.status >= 400 && error.status < 500),
      },
      mutations: { retry: false },
    },
  });
}

export function Providers({ config, children }: { config: RuntimeConfig; children: ReactNode }) {
  const [queryClient] = useState(makeQueryClient);
  const [api] = useState(() => createApiClient(config.apiUrl));
  return (
    <ConfigProvider value={config}>
      <ApiContext.Provider value={api}>
        <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
      </ApiContext.Provider>
    </ConfigProvider>
  );
}
