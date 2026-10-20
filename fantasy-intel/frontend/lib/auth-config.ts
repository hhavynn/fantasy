// Sign-in configuration and the small pure decisions around it (Supabase Auth, email link). No Next, no network:
// node --test covers them. The Supabase client itself lives in lib/supabase/*.ts (server only).

export type AuthConfig = { url: string; key: string; signupsOpen: boolean };

/**
 * Sign-in is on only with the multi-league UI flag and both Supabase settings. The URL and the publishable key are
 * read on the server only (sign-in runs in server actions and the proxy), so nothing here reaches the browser.
 * New accounts are refused until SIGNUPS_OPEN=1 (Phase 6's launch gate): until then only existing users sign in.
 */
export function authConfig(env: Record<string, string | undefined>): AuthConfig | null {
  if (env.MULTILEAGUE_UI_ENABLED !== "1") return null;
  const url = (env.SUPABASE_URL ?? "").trim().replace(/\/+$/, "");
  const key = (env.SUPABASE_PUBLISHABLE_KEY ?? "").trim();
  if (!/^https:\/\/[a-z0-9-]+\.supabase\.co$/.test(url) || !key) return null;
  return { url, key, signupsOpen: env.SIGNUPS_OPEN === "1" };
}

/** Where to go after signing in: an in-app path only (never another origin), else the leagues page. */
export function safeNext(value: string | null | undefined): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.startsWith("/\\")) return "/leagues";
  return value;
}

/** Supabase's session cookies are named sb-<project ref>-auth-token (possibly chunked: …-auth-token.0, .1). */
export function hasSessionCookie(names: readonly string[]): boolean {
  return names.some((n) => /^sb-[a-z0-9]+-auth-token(\.\d+)?$/.test(n));
}

/** The origin the email link must come back to: SITE_URL when set (production), else the request's own host. */
export function siteOrigin(get: (name: string) => string | null, env: Record<string, string | undefined>): string {
  const configured = (env.SITE_URL ?? "").trim().replace(/\/+$/, "");
  if (/^https?:\/\/[^/]+$/.test(configured)) return configured;
  const host = get("x-forwarded-host") ?? get("host") ?? "localhost:3000";
  const proto = get("x-forwarded-proto") ?? (host.startsWith("localhost") ? "http" : "https");
  return `${proto}://${host}`;
}

export const EMAIL_RE = /^[^\s@]{1,64}@[^\s@]{1,190}\.[^\s@]{2,}$/;
