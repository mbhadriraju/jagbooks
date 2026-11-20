import "server-only";

export function fastApiBinding() {
  const address = process.env.JAGBOOKS_FASTAPI_BINDING;
  if (address) return address.replace(/\/+$/, "");

  // Supports running Next.js and Uvicorn separately for local development.
  if (process.env.NODE_ENV === "development" && !process.env.VERCEL) {
    return "http://127.0.0.1:8000";
  }
}
