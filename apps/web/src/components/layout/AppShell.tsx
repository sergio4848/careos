"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { useSession } from "@/lib/auth/SessionProvider";
import { useConfig } from "@/lib/config";
import { useRealtimeStatus } from "@/lib/realtime/RealtimeProvider";

import styles from "./AppShell.module.css";
import { ConnectionIndicator } from "./ConnectionIndicator";

interface NavItem {
  href: string;
  label: string;
  permission: string;
  devOnly?: boolean;
}

const NAV: NavItem[] = [
  { href: "/dashboard", label: "Operations", permission: "dashboard:read" },
  { href: "/dev/sos-simulator", label: "SOS Simulator", permission: "simulator:use", devOnly: true },
  { href: "/audit", label: "Audit trail", permission: "audit:read" },
];

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const { session, can, logout } = useSession();
  const { simulatorEnabled } = useConfig();
  const { announcement } = useRealtimeStatus();

  const items = NAV.filter((item) => can(item.permission) && (!item.devOnly || simulatorEnabled));

  return (
    <div className={styles.shell}>
      <a className={styles.skipLink} href="#main">
        Skip to main content
      </a>
      <header className={styles.topbar}>
        <div className={styles.brand}>
          <span className={styles.logo} aria-hidden="true">
            ◆
          </span>
          <span className={styles.wordmark}>CareOS</span>
          <span className={styles.org}>{session.organisation?.name ?? "Platform"}</span>
        </div>
        <nav aria-label="Primary" className={styles.nav}>
          {items.map((item) => {
            const active = pathname.startsWith(item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`${styles.navLink} ${active ? styles.active : ""}`}
                aria-current={active ? "page" : undefined}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>
        <div className={styles.right}>
          <ConnectionIndicator />
          <div className={styles.user}>
            <span className={styles.userName}>{session.user.full_name}</span>
            <span className={styles.userRole}>{session.user.role.replace("_", " ")}</span>
          </div>
          <button type="button" className={styles.signOut} onClick={() => void logout()}>
            Sign out
          </button>
        </div>
      </header>
      <div aria-live="assertive" aria-atomic="true" className="visually-hidden">
        {announcement}
      </div>
      <main id="main" className={styles.main}>
        {children}
      </main>
    </div>
  );
}
