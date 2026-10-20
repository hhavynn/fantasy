import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { LEAGUE_COOKIE, LEAGUE_HEADER, proxyDecision } from "./lib/league-scope";
import { basicAuthOk } from "./lib/site-auth";

// 1. Hosted mode: when SITE_PASSWORD is set, every page asks for it (browser Basic auth prompt). Locally unset: no-op.
// 2. Multi-league (MULTILEAGUE_UI_ENABLED=1): /l/<lg>/<page> serves <page> for that league (lib/league-scope.ts).
//    With the flag unset this does nothing, and the site behaves exactly as before.
const COOKIE_MAX_AGE = 60 * 60 * 24 * 180;

export function proxy(request: NextRequest) {
  const password = process.env.SITE_PASSWORD;
  if (password && !basicAuthOk(request.headers.get("authorization"), password)) {
    return new NextResponse("Password required", {
      status: 401,
      headers: { "WWW-Authenticate": 'Basic realm="fantasy-intel", charset="UTF-8"' },
    });
  }
  const decision = proxyDecision({
    enabled: process.env.MULTILEAGUE_UI_ENABLED === "1",
    pathname: request.nextUrl.pathname,
    search: request.nextUrl.search,
    cookie: request.cookies.get(LEAGUE_COOKIE)?.value ?? null,
  });
  switch (decision.kind) {
    case "rewrite": {
      const headers = new Headers(request.headers);
      headers.set(LEAGUE_HEADER, decision.league);
      const response = NextResponse.rewrite(new URL(decision.path, request.url), { request: { headers } });
      response.cookies.set(LEAGUE_COOKIE, decision.league, { path: "/", sameSite: "lax", maxAge: COOKIE_MAX_AGE, httpOnly: true });
      return response;
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
      if (!request.headers.has(LEAGUE_HEADER)) return NextResponse.next();
      const headers = new Headers(request.headers);
      headers.delete(LEAGUE_HEADER);
      return NextResponse.next({ request: { headers } });
    }
  }
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
