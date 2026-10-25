import { createServerClient } from "@supabase/ssr";
import type { NextRequest, NextResponse } from "next/server";
import { authConfig, hasSessionCookie } from "../auth-config";

/**
 * Refresh an expiring Supabase session on the way through the proxy and copy the new cookies onto `response`.
 * Only requests that already carry a session cookie pay for it: anonymous visitors never reach Supabase here.
 */
export async function refreshSession(request: NextRequest, response: NextResponse): Promise<NextResponse> {
  const config = authConfig(process.env);
  if (!config || !hasSessionCookie(request.cookies.getAll().map((c) => c.name))) return response;
  const supabase = createServerClient(config.url, config.key, {
    cookies: {
      getAll: () => request.cookies.getAll(),
      setAll: (list) => {
        for (const { name, value, options } of list) {
          request.cookies.set(name, value);
          response.cookies.set(name, value, options);
        }
      },
    },
  });
  try {
    await supabase.auth.getClaims();      // refreshes the access token when it has expired
  } catch {
    // An unreachable Supabase must not take the site down: the page renders signed out.
  }
  return response;
}
