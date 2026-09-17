import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import { Providers } from "@/lib/providers";
import { readRuntimeConfig } from "@/lib/runtime-config";

import "./globals.css";

// Runtime configuration (API URL, feature flags) is read per request, so one image runs everywhere.
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: { default: "CareOS Operations", template: "%s · CareOS" },
  description: "Telecare and safety incident operations console",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  themeColor: "#0a1020",
  colorScheme: "dark",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  const config = readRuntimeConfig();
  return (
    <html lang="en-GB">
      <body>
        <Providers config={config}>{children}</Providers>
      </body>
    </html>
  );
}
