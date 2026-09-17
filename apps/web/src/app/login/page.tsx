"use client";

import type { SessionView } from "@careos/contracts";
import { useQueryClient } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState, type FormEvent } from "react";

import { Button } from "@/components/ui/Button";
import { isApiError } from "@/lib/api/client";
import { queryKeys } from "@/lib/api/queries";
import { useConfig } from "@/lib/config";
import { useApi } from "@/lib/providers";

import styles from "./login.module.css";

const DEMO_ACCOUNTS = [
  ["operator@democare.example.com", "Operator"],
  ["operator2@democare.example.com", "Second operator (race demo)"],
  ["manager@democare.example.com", "Care manager (audit trail)"],
  ["operator@northshire.example.com", "Other organisation (isolation demo)"],
] as const;

function safeNext(next: string | null): string {
  return next && next.startsWith("/") && !next.startsWith("//") ? next : "/dashboard";
}

function LoginForm() {
  const api = useApi();
  const router = useRouter();
  const params = useSearchParams();
  const queryClient = useQueryClient();
  const { simulatorEnabled } = useConfig();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const session = await api.post<SessionView>("/v1/auth/login", { email, password });
      api.setCsrfToken(session.csrf_token);
      queryClient.setQueryData(queryKeys.session, session);
      router.replace(safeNext(params.get("next")));
    } catch (err) {
      setError(
        isApiError(err, 429)
          ? "Too many attempts. Please wait a few minutes and try again."
          : isApiError(err)
            ? err.message
            : "Unable to sign in.",
      );
      setSubmitting(false);
    }
  };

  return (
    <main className={styles.page}>
      <section className={styles.card} aria-labelledby="login-title">
        <div className={styles.brand}>
          <span className={styles.logo} aria-hidden="true">
            ◆
          </span>
          CareOS
        </div>
        <h1 id="login-title">Operations console sign in</h1>
        <form onSubmit={onSubmit} className={styles.form}>
          <label>
            <span>Email</span>
            <input
              type="email"
              autoComplete="username"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              required
            />
          </label>
          <label>
            <span>Password</span>
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
          </label>
          {error ? (
            <p role="alert" className={styles.error}>
              {error}
            </p>
          ) : null}
          <Button type="submit" variant="primary" size="lg" loading={submitting}>
            Sign in
          </Button>
        </form>
        {simulatorEnabled ? (
          <aside className={styles.demo} aria-label="Demo accounts">
            <p className="eyebrow">Development demo accounts</p>
            <ul>
              {DEMO_ACCOUNTS.map(([account, label]) => (
                <li key={account}>
                  <button type="button" onClick={() => setEmail(account)}>
                    {account}
                  </button>
                  <span>{label}</span>
                </li>
              ))}
            </ul>
            <p className={styles.hint}>Password: value of CAREOS_SEED_DEMO_PASSWORD.</p>
          </aside>
        ) : null}
      </section>
    </main>
  );
}

export default function LoginPage() {
  return (
    <Suspense>
      <LoginForm />
    </Suspense>
  );
}
