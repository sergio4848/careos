"use client";

import type { ReactNode } from "react";

import { AppShell } from "@/components/layout/AppShell";
import { SessionProvider, useSession } from "@/lib/auth/SessionProvider";
import { RealtimeProvider } from "@/lib/realtime/RealtimeProvider";

function RealtimeWithSession({ children }: { children: ReactNode }) {
  const { expire } = useSession();
  return <RealtimeProvider onUnauthorised={expire}>{children}</RealtimeProvider>;
}

export default function ConsoleLayout({ children }: { children: ReactNode }) {
  return (
    <SessionProvider fallback={<p className="visually-hidden">Checking your session…</p>}>
      <RealtimeWithSession>
        <AppShell>{children}</AppShell>
      </RealtimeWithSession>
    </SessionProvider>
  );
}
