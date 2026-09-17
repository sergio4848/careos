import type { RuntimeConfig } from "./config";

/** Server-only: resolve public runtime configuration from the environment. */
export function readRuntimeConfig(env: NodeJS.ProcessEnv = process.env): RuntimeConfig {
  const apiUrl = (env.CAREOS_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/+$/, "");
  const wsUrl = env.CAREOS_PUBLIC_WS_URL ?? `${apiUrl.replace(/^http/, "ws")}/v1/ws`;
  return {
    apiUrl,
    wsUrl,
    simulatorEnabled: (env.CAREOS_SIMULATOR_ENABLED ?? "true").toLowerCase() === "true",
  };
}
