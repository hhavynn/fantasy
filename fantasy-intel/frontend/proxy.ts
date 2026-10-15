import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { basicAuthOk } from "./lib/site-auth";

// Hosted mode: when SITE_PASSWORD is set, every page asks for it (browser Basic auth prompt).
// Locally it is unset and this is a no-op.
export function proxy(request: NextRequest) {
  const password = process.env.SITE_PASSWORD;
  if (!password || basicAuthOk(request.headers.get("authorization"), password)) return NextResponse.next();
  return new NextResponse("Password required", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="fantasy-intel", charset="UTF-8"' },
  });
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
