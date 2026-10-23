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
  // A local Supabase (the Supabase CLI's http://127.0.0.1:54321, the browser tests' JWKS) only with SUPABASE_LOCAL=1, and
  // never on Vercel: a deployment always talks to its hosted project over https.
  const local = env.SUPABASE_LOCAL === "1" && !env.VERCEL && /^http:\/\/(127\.0\.0\.1|localhost):\d{2,5}$/.test(url);
  if ((!local && !/^https:\/\/[a-z0-9-]+\.supabase\.co$/.test(url)) || !key) return null;
  return { url, key, signupsOpen: env.SIGNUPS_OPEN === "1" };
}

const NEXT_FALLBACK = "/leagues";
const DUMMY_ORIGIN = "http://site.invalid";

/**
 * Where to go after signing in: an in-app path only (never another origin), else the leagues page. Returned in the
 * URL parser's normalized form, the form a browser or `new URL()` would resolve, so what is checked is what is used.
 * URL parsers drop tabs and newlines, read `\` as `/` and resolve dot segments, so `/\t/evil.example`,
 * `/\evil.example` and `/.//evil.example` (also `%2e`-encoded) would all become `//evil.example`, another host: any
 * control character or backslash and any `.` or `..` segment are refused, and so is a result starting with `//`.
 */
export function safeNext(value: string | null | undefined): string {
  if (!value || value.length > 2048 || !value.startsWith("/") || value.startsWith("//") || /[\\\u0000-\u001f\u007f]/.test(value)) {
    return NEXT_FALLBACK;
  }
  for (const segment of value.split(/[?#]/)[0].split("/")) {
    let decoded: string;
    try {
      decoded = decodeURIComponent(segment);
    } catch {
      return NEXT_FALLBACK;                    // a malformed escape
    }
    if (decoded === "." || decoded === "..") return NEXT_FALLBACK;
  }
  let url: URL;
  try {
    url = new URL(value, DUMMY_ORIGIN);
  } catch {
    return NEXT_FALLBACK;
  }
  const normalized = url.pathname + url.search + url.hash;
  return url.origin === DUMMY_ORIGIN && !normalized.startsWith("//") ? normalized : NEXT_FALLBACK;
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
