// Paths that belong to no league, so the proxy never rewrites or redirects them (lib/league-scope.ts): Next internals,
// the backend proxy routes, the /leagues connections page, account pages (sign-in, sign-up, their email callback,
// invites, account, admin), the terms and privacy policy, and site icons and metadata. Plain JavaScript with no imports, so
// lib/league-scope.ts, eslint.config.mjs (its redirect() rule allows these targets) and node's test runner all load
// this one list.
export const UNSCOPED_PREFIXES = ["/_next", "/api", "/leagues", "/login", "/signup", "/auth", "/invite", "/account", "/admin", "/terms",
  "/privacy", "/favicon", "/icon", "/apple-icon", "/robots", "/sitemap"];
