import { syncServerClock } from "../time/server-clock";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details: Record<string, unknown> = {},
    readonly requestId: string | null = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export interface ApiClient {
  get<T>(path: string, signal?: AbortSignal): Promise<T>;
  post<T>(path: string, body?: unknown): Promise<T>;
  put<T>(path: string, body: unknown): Promise<T>;
  setCsrfToken(token: string | null): void;
}

interface ErrorEnvelope {
  error?: { code?: string; message?: string; details?: Record<string, unknown>; request_id?: string };
}

/**
 * Thin fetch wrapper for the CareOS API.
 * - Session cookie is HttpOnly and sent with `credentials: "include"`; JS never sees it.
 * - The CSRF token (from /v1/auth/login or /v1/auth/me) is kept in memory only.
 */
export function createApiClient(baseUrl: string, fetchImpl: typeof fetch = fetch): ApiClient {
  let csrfToken: string | null = null;

  async function request<T>(method: Method, path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;

    let response: Response;
    try {
      response = await fetchImpl(`${baseUrl}${path}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: "include",
        cache: "no-store",
        signal,
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      throw new ApiError(0, "network_error", "The CareOS API could not be reached.");
    }
    syncServerClock(response.headers.get("date"));

    if (response.status === 204) return undefined as T;
    const payload: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      const envelope = (payload ?? {}) as ErrorEnvelope;
      throw new ApiError(
        response.status,
        envelope.error?.code ?? "http_error",
        envelope.error?.message ?? `Request failed with status ${response.status}`,
        envelope.error?.details ?? {},
        envelope.error?.request_id ?? response.headers.get("x-request-id"),
      );
    }
    return payload as T;
  }

  return {
    get: (path, signal) => request("GET", path, undefined, signal),
    post: (path, body) => request("POST", path, body ?? {}),
    put: (path, body) => request("PUT", path, body),
    setCsrfToken: (token) => {
      csrfToken = token;
    },
  };
}

export function isApiError(error: unknown, status?: number): error is ApiError {
  return error instanceof ApiError && (status === undefined || error.status === status);
}
