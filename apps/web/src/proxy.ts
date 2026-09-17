import { NextResponse } from "next/server";

import { readRuntimeConfig } from "./lib/runtime-config";

/**
 * Adds a Content-Security-Policy whose connect-src matches the runtime API/WebSocket URLs.
 * (next.config headers are fixed at build time; this runs per request.)
 */
export function proxy() {
  const { apiUrl, wsUrl } = readRuntimeConfig();
  const isDev = process.env.NODE_ENV !== "production";
  const csp = [
    "default-src 'self'",
    `script-src 'self' 'unsafe-inline'${isDev ? " 'unsafe-eval'" : ""}`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self'",
    `connect-src 'self' ${apiUrl} ${new URL(wsUrl).origin}${isDev ? " ws: wss:" : ""}`,
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
  ].join("; ");
  const response = NextResponse.next();
  response.headers.set("Content-Security-Policy", csp);
  return response;
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico|icon.svg).*)"],
};
