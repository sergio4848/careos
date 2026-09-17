"use client";

import { createContext, useContext, type ReactNode } from "react";

/** Values read from the environment at request time on the server (see app/layout.tsx). */
export interface RuntimeConfig {
  apiUrl: string;
  wsUrl: string;
  simulatorEnabled: boolean;
}

const ConfigContext = createContext<RuntimeConfig | null>(null);

export function ConfigProvider({ value, children }: { value: RuntimeConfig; children: ReactNode }) {
  return <ConfigContext.Provider value={value}>{children}</ConfigContext.Provider>;
}

export function useConfig(): RuntimeConfig {
  const config = useContext(ConfigContext);
  if (!config) throw new Error("useConfig must be used inside <ConfigProvider>");
  return config;
}
