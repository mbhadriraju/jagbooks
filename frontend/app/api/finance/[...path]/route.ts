import { fastApiBinding } from "@/lib/fastapi-binding";
import { NextRequest } from "next/server";

const id = "[0-9a-fA-F-]{36}";
const routes: Record<string, RegExp> = {
  GET: new RegExp(`^/(finance/access|sheets|sheets/${id}/workbook(?:/preview)?|sheets/${id}/files|files/${id}/download)$`),
  POST: new RegExp(`^/(sheets|sheets/${id}/(?:files|workbook/initialize)|files/${id}/review)$`),
  PUT: new RegExp(`^/(sheets/${id}/workbook|files/${id}/review)$`),
};

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const path = "/" + (await context.params).path.join("/");
  if (!routes[request.method]?.test(path)) return new Response(null, { status: 404 });
  const base = fastApiBinding();
  if (!base) return Response.json({ detail: "Finance backend is unavailable." }, { status: 503 });
  const headers = new Headers();
  for (const key of ["authorization", "content-type"]) {
    const value = request.headers.get(key);
    if (value) headers.set(key, value);
  }
  // Stream uploads to FastAPI, where file size and signature are checked.
  const init: RequestInit & { duplex: "half" } = {
    method: request.method, headers, body: request.body, duplex: "half",
    cache: "no-store", redirect: "error", signal: AbortSignal.timeout(260_000),
  };
  const query = new URLSearchParams(request.nextUrl.search);
  if (request.method === "POST" && path.endsWith("/files")) query.set("review", "true");
  try {
    const response = await fetch(base + path + "?" + query, init);
    const outgoing = new Headers({ "Cache-Control": "private, no-store" });
    for (const key of ["content-type", "content-disposition", "x-workbook-version"]) {
      const value = response.headers.get(key);
      if (value) outgoing.set(key, value);
    }
    return new Response(response.body, { status: response.status, headers: outgoing });
  } catch {
    return Response.json({ detail: "Finance service could not complete the request." }, { status: 502 });
  }
}

export const maxDuration = 300;
export { proxy as GET, proxy as POST, proxy as PUT };
