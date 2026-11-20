import { fastApiBinding } from "@/lib/fastapi-binding";
import { connection } from "next/server";

type BrowserConfig = { url: string; key: string };

function validConfig(url: unknown, key: unknown): BrowserConfig | null {
  if (typeof url !== "string" || typeof key !== "string" || !/^sb_publishable_[\w-]+$/.test(key)) return null;
  try {
    const target = new URL(url);
    if (target.protocol === "https:" && target.origin === url.replace(/\/$/, "")) {
      return { url: target.origin, key };
    }
  } catch {
    return null;
  }
  return null;
}

export async function GET() {
  await connection();
  const headers = { "Cache-Control": "no-store" };
  const configured = validConfig(
    process.env.NEXT_PUBLIC_SUPABASE_URL || process.env.SUPABASE_URL,
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY || process.env.SUPABASE_PUBLISHABLE_KEY,
  );
  if (configured) return Response.json(configured, { headers });

  const backend = fastApiBinding();
  if (backend) {
    try {
      const endpoint = backend + "/auth/browser-config";
      const response = await fetch(endpoint, {
        cache: "no-store", redirect: "error", signal: AbortSignal.timeout(5000),
      });
      if (response.ok) {
        const { url, key } = await response.json();
        const config = validConfig(url, key);
        if (config) return Response.json(config, { headers });
      }
    } catch {
      // The browser receives fixed text regardless of a private service error.
    }
  }
  return Response.json({ detail: "Google sign-in is unavailable. The site needs its Supabase publishable configuration." }, { status: 503, headers });
}
