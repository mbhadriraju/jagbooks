"use client";

import { createClient, type Session, type SupabaseClient } from "@supabase/supabase-js";
import Link from "next/link";
import Image from "next/image";
import { usePathname } from "next/navigation";
import { Suspense, createContext, useContext, useEffect, useState } from "react";

let clientPromise: Promise<SupabaseClient> | undefined;
let callbackPromise: Promise<string> | undefined;

function getClient() {
  clientPromise ??= (async () => {
    const response = await fetch("/api/auth/config", { cache: "no-store" });
    if (!response.ok) {
      throw new Error("Google sign-in is unavailable. The site needs its Supabase publishable configuration.");
    }
    const { url, key } = await response.json();
    return createClient(url, key, {
      auth: { flowType: "pkce", detectSessionInUrl: false, storageKey: "bvw-finance-auth" },
    });
  })().catch((error) => {
    clientPromise = undefined;
    throw error;
  });
  return clientPromise;
}

function completeCallback(client: SupabaseClient) {
  callbackPromise ??= (async () => {
    const current = new URL(window.location.href);
    if (current.pathname !== "/analysis") return "";
    const code = current.searchParams.get("code");
    const rejected = current.searchParams.has("error");
    if (!code && !rejected) return "";
    let error = rejected;
    if (code && !rejected) {
      try {
        error = Boolean((await client.auth.exchangeCodeForSession(code)).error);
      } catch {
        error = true;
      }
    }
    for (const field of ["code", "error", "error_code", "error_description"]) current.searchParams.delete(field);
    window.history.replaceState(null, "", current.pathname + current.search + current.hash);
    return error ? "Google sign-in could not be completed. Choose Continue with Google to try again." : "";
  })();
  return callbackPromise;
}

const Auth = createContext<Session | null | undefined>(undefined);
export const useSession = () => useContext(Auth);

export function FinanceShell({ children }: { children: React.ReactNode }) {
  const [session, setSession] = useState<Session | null>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let unsubscribe: (() => void) | undefined;
    getClient().then(async (client) => {
      const failure = await completeCallback(client);
      if (cancelled) return;
      if (failure) setError(failure);
      const { data } = client.auth.onAuthStateChange((_, current) => setSession(current));
      unsubscribe = () => data.subscription.unsubscribe();
    }).catch(() => {
      if (!cancelled) setSession(null);
    });
    return () => { cancelled = true; unsubscribe?.(); };
  }, []);

  async function changeSession() {
    setBusy(true);
    setError("");
    try {
      const client = await getClient();
      if (session) {
        const { error } = await client.auth.signOut({ scope: "local" });
        if (error) throw new Error("Could not sign out. Try again.");
        setSession(null);
      } else {
        const { data, error } = await client.auth.signInWithOAuth({
          provider: "google",
          options: {
            redirectTo: new URL("/analysis", window.location.origin).href,
            skipBrowserRedirect: true,
            queryParams: { prompt: "select_account" },
          },
        });
        if (error || !data.url) throw new Error("Could not start Google sign-in. Try again.");
        window.location.assign(data.url);
        return;
      }
    } catch (problem) {
      setError(problem instanceof Error && problem.message.startsWith("Google sign-in is unavailable.") ? problem.message : session ? "Could not sign out. Try again." : "Could not start Google sign-in. Try again.");
    }
    setBusy(false);
  }

  return (
    <Auth.Provider value={session}>
      <header className="site-header">
        <div className="wrap topbar">
          <Link href="/" className="wordmark"><Image src="/icon.png" alt="" width={36} height={36} unoptimized />JagBooks</Link>
          <Suspense fallback={<Navigation />}><CurrentNavigation /></Suspense>
          <button className="button account" disabled={session === undefined || busy} onClick={() => void changeSession()}>
            {busy ? (session ? "Signing out…" : "Opening Google…") : session ? "Sign out" : "Continue with Google"}
          </button>
        </div>
      </header>
      {error && <p className="wrap notice error" role="alert">{error}</p>}
      {children}
    </Auth.Provider>
  );
}

function CurrentNavigation() {
  return <Navigation path={usePathname()} />;
}

function Navigation({ path }: { path?: string }) {
  return (
    <nav aria-label="Main navigation">
      <Link href="/" aria-current={path === "/" ? "page" : undefined}>Home</Link>
      <Link href="/analysis" aria-current={path?.startsWith("/analysis") ? "page" : undefined}>Analysis</Link>
    </nav>
  );
}

export function LoginNotice() {
  return <p>Continue with Google to view private financial statements.</p>;
}

export const XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

export async function api(path: string, token: string, options: RequestInit = {}) {
  const headers = new Headers(options.headers);
  headers.set("Authorization", "Bearer " + token);
  const response = await fetch("/api/finance" + path, {
    ...options, headers, cache: "no-store", credentials: "omit", redirect: "error",
  }).catch(() => { throw new Error("Could not reach the finance service. Try again."); });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = body?.detail;
    const message = typeof detail === "string" ? detail : detail?.message;
    throw new Error(message || (response.status === 413 ? "File exceeds the upload limit." : "Finance request failed. Try again."));
  }
  return response;
}

export const currency = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
export const money = (cents: number) => currency.format(cents / 100);
