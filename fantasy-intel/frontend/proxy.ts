import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { LEAGUE_COOKIE, LEAGUE_HEADER, proxyDecision, type ProxyDecision } from "./lib/league-scope";
import { FixedWindowLimiter, clientIp, limitFromEnv } from "./lib/rate-limit";
import { basicAuthOk } from "./lib/site-auth";
import { refreshSession } from "./lib/supabase/proxy";

// 1. Hosted mode: when SITE_PASSWORD is set, every page asks for it (browser Basic auth prompt). Locally unset: no-op.
// 2. Multi-league (MULTILEAGUE_UI_ENABLED=1): /l/<lg>/<page> serves <page> for that league (lib/league-scope.ts),
//    and a signed-in visitor's Supabase session is refreshed on the way (lib/supabase/proxy.ts).
//    With the flag unset this does nothing, and the site behaves exactly as before.
// 3. Abuse limit (Phase 6, RATE_LIMIT_PER_MINUTE): requests per client IP per minute in this instance; off when unset.
//    Every visitor is counted, signed in or not: a session cookie is not verified here, so it cannot buy a higher limit.
const COOKIE_MAX_AGE = 60 * 60 * 24 * 180;
const perMinute = limitFromEnv(process.env.RATE_LIMIT_PER_MINUTE);
const limiter = perMinute ? new FixedWindowLimiter(perMinute, 60_000) : null;

export async function proxy(request: NextRequest) {
  const password = process.env.SITE_PASSWORD;
  if (password && !basicAuthOk(request.headers.get("authorization"), password)) {
    return new NextResponse("Password required", {
      status: 401,
      headers: { "WWW-Authenticate": 'Basic realm="fantasy-intel", charset="UTF-8"' },
    });
  }
  const ip = limiter ? clientIp((name) => request.headers.get(name)) : null;
  const verdict = limiter && ip ? limiter.check(ip, Date.now()) : null;
  if (verdict && !verdict.ok) {
    return new NextResponse("Too many requests. Try again in a minute.", {
      status: 429,
      headers: { "Retry-After": String(verdict.retryAfterSeconds), "Cache-Control": "no-store" },
    });
  }
  const decision = proxyDecision({
    enabled: process.env.MULTILEAGUE_UI_ENABLED === "1",
    pathname: request.nextUrl.pathname,
    search: request.nextUrl.search,
    cookie: request.cookies.get(LEAGUE_COOKIE)?.value ?? null,
    classic: process.env.CLASSIC_SITE_ENABLED !== "0",
  });
  // Defence in depth: a rewrite or redirect target is always this site. lib/league-scope.ts never builds one that is
  // not (splitLeaguePath rejects `/l/<lg>//host`), but `new URL()` would follow a `//host` path to another origin.
  // Such a target is answered like any unmatched path (the default branch below). Both sides resolve against
  // request.url, so only a target that names another host differs.
  const target = decision.kind === "next" ? null
    : new URL(decision.kind === "rewrite" ? decision.path : decision.location, request.url);
  const d: ProxyDecision = target && target.origin !== new URL(request.url).origin ? { kind: "next" } : decision;
  switch (d.kind) {
    case "rewrite": {
      const headers = new Headers(request.headers);
      headers.set(LEAGUE_HEADER, d.league);
      const response = NextResponse.rewrite(target!, { request: { headers } });
      response.cookies.set(LEAGUE_COOKIE, d.league, { path: "/", sameSite: "lax", maxAge: COOKIE_MAX_AGE, httpOnly: true });
      return refreshSession(request, response);
    }
    case "redirect":   // also /l/<lg>/<page>?league=clear -> /l/<lg>/<page> without it
      return NextResponse.redirect(target!, 307);
    case "clear": {
      const response = NextResponse.redirect(target!, 307);
      response.cookies.delete(LEAGUE_COOKIE);
      return response;
    }
    default: {
      // Never let a client send its own league header to an unscoped page.
      if (!request.headers.has(LEAGUE_HEADER)) return refreshSession(request, NextResponse.next({ request }));
      const headers = new Headers(request.headers);
      headers.delete(LEAGUE_HEADER);
      return refreshSession(request, NextResponse.next({ request: { headers } }));
    }
  }
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
