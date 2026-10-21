import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { LEAGUE_COOKIE, LEAGUE_HEADER, proxyDecision } from "./lib/league-scope";
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
  switch (decision.kind) {
    case "rewrite": {
      const headers = new Headers(request.headers);
      headers.set(LEAGUE_HEADER, decision.league);
      const response = NextResponse.rewrite(new URL(decision.path, request.url), { request: { headers } });
      response.cookies.set(LEAGUE_COOKIE, decision.league, { path: "/", sameSite: "lax", maxAge: COOKIE_MAX_AGE, httpOnly: true });
      return refreshSession(request, response);
    }
    case "redirect":
      return NextResponse.redirect(new URL(decision.location, request.url), 307);
    case "clear": {
      const response = NextResponse.redirect(new URL(decision.location, request.url), 307);
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
