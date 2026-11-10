"use client";

import { useEffect, useState } from "react";

interface AuthState {
  authenticated: boolean;
  deployment_mode?: "production" | "fixture";
  role?: string;
  kind?: string;
  csrf_token?: string;
  memory_consent?: boolean;
  migration_available?: boolean;
  migration?: { id: string; status: string };
}

/** Authentication is a BFF cookie flow; no token enters JavaScript or browser storage. */
export function AuthControls() {
  const [state, setState] = useState<AuthState | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [pending, setPending] = useState(false);
  const [consent, setConsent] = useState(false);
  const production = process.env.NODE_ENV === "production";

  useEffect(() => {
    const report = (event: Event) => setError(String((event as CustomEvent).detail));
    window.addEventListener("commerce-api-error", report);
    const inform = (event: Event) => setNotice(String((event as CustomEvent).detail));
    window.addEventListener("commerce-api-notice", inform);
    if (production) {
      void fetch("/api/auth/session", { credentials: "same-origin", cache: "no-store" })
        .then(async (response) => {
          if (!response.ok) throw new Error("Your session has expired. Sign in again.");
          setState(await response.json());
        }).catch((failure) => setError(failure.message));
    }
    return () => {
      window.removeEventListener("commerce-api-error", report);
      window.removeEventListener("commerce-api-notice", inform);
    };
  }, [production]);

  useEffect(() => {
    const migration = state?.migration;
    if (!migration || migration.status === "complete") return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let attempts = 0;
    const check = async () => {
      try {
        const response = await fetch(`/api/auth/migrations/${encodeURIComponent(migration.id)}/status`, {
          method: "POST", credentials: "same-origin", signal: controller.signal,
          headers: { "X-CSRF-Token": state?.csrf_token ?? "" },
        });
        if (!response.ok) throw new Error("Unable to check migration. Your preferences have not been reported as imported.");
        const result = await response.json();
        if (result.status === "complete") {
          setState((previous) => previous ? { ...previous, migration: { id: migration.id, status: "complete" } } : previous);
          setNotice("Guest preferences imported. Existing account preferences were preserved.");
          return;
        }
        if (["failed", "dead", "cancelled", "uncertain"].includes(result.status)) {
          setError("Preference import needs attention. It has not been reported as complete.");
          return;
        }
        if (++attempts < 60) timer = setTimeout(() => void check(), 2000);
        else setNotice("Preference import is still pending. Check its status again later.");
      } catch (failure) {
        if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : "Migration status check failed.");
      }
    };
    void check();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [state?.migration?.id, state?.migration?.status, state?.csrf_token]);

  async function act(path: string, body?: unknown) {
    setPending(true);
    setError("");
    try {
      const response = await fetch(path, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", ...(state?.csrf_token ? { "X-CSRF-Token": state.csrf_token } : {}) },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      if (!response.ok) throw new Error(`Identity operation failed (${response.status}). Nothing was silently merged.`);
      window.location.reload();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "The request failed.");
    } finally { setPending(false); }
  }

  if (!production && !error && !notice) return null;
  return <section aria-label="Identity and privacy" className="shrink-0 border-b border-(--line) bg-(--card) p-3 text-sm text-(--ink)">
    {state?.deployment_mode === "fixture" && <p role="status">
      Sample-data demo. No real orders, payments, or fulfillment. Catalog and commerce state can reset on restart and differ between roles.
    </p>}
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {production && <>
      <a href="/auth/login" className="underline">{state?.kind === "customer" || state?.kind === "merchant" ? "Switch account" : "Sign in"}</a>
      {state && !state.authenticated && state.role === "shopping" && <div>
        <p>Continue as a guest. Memory is optional and expires after inactivity.</p>
        <label><input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} /> Remember my preferences for future visits</label>{" "}
        <button disabled={pending} onClick={() => void act("/api/auth/guest/bootstrap", { memory_consent: consent })}>Continue as guest{consent ? " with memory" : " without memory"}</button>
      </div>}
      {state?.kind === "guest" && <p>Guest memory: {state.memory_consent ? "enabled by consent" : "off"}. Sign in to manage account data.</p>}
      {state?.migration_available && <div>
        <p>Your guest preferences are separate. Existing account preferences always win conflicts; conversations, orders, and carts are not merged.</p>
        <label><input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} /> I consent to copy eligible guest preferences into this account</label>{" "}
        <button disabled={pending || !consent} onClick={() => void act("/api/auth/migrate", { consent: true })}>Import guest preferences</button>
      </div>}
      {state?.migration && <p role="status">
        Preference import: {state.migration.status === "complete" ? "complete" : "pending; not yet confirmed complete"}.
        {state.migration.status !== "complete" && <button onClick={() => window.location.reload()}>Check migration status again</button>}
      </p>}
      {(state?.authenticated || error) && <button disabled={pending} onClick={() => void act("/auth/logout")}>Sign out and revoke this browser session</button>}
    </>}
  </section>;
}
