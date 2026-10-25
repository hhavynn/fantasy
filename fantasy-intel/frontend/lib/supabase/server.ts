import "server-only";
import { createServerClient } from "@supabase/ssr";
import { cookies } from "next/headers";
import { authConfig } from "../auth-config";

// The Supabase client for server components, server actions and route handlers. Sessions live in Supabase's own
// httpOnly cookies (set through Next's cookie store); the access token is forwarded to the API as a bearer token and
// verified there (backend/app/control_plane/jwt_auth.py). Null when sign-in is off.

export async function supabaseServer() {
  const config = authConfig(process.env);
  if (!config) return null;
  const store = await cookies();
  return createServerClient(config.url, config.key, {
    cookies: {
      getAll: () => store.getAll(),
      setAll: (list) => {
        try {
          for (const { name, value, options } of list) store.set(name, value, options);
        } catch {
          // A server component cannot set cookies; the proxy refreshes the session on the next request.
        }
      },
    },
  });
}

/** The signed-in user's access token, or null (signed out, sign-in off, or an unreadable session). */
export async function accessToken(): Promise<string | null> {
  const supabase = await supabaseServer();
  if (!supabase) return null;
  try {
    const { data } = await supabase.auth.getSession();
    return data.session?.access_token ?? null;
  } catch {
    return null;
  }
}

export type SignedInUser = { id: string; email: string | null };

/** The signed-in user from the verified token claims, or null. */
export async function currentUser(): Promise<SignedInUser | null> {
  const supabase = await supabaseServer();
  if (!supabase) return null;
  try {
    const { data } = await supabase.auth.getClaims();
    const claims = data?.claims;
    if (!claims?.sub || claims.is_anonymous) return null;
    return { id: String(claims.sub), email: typeof claims.email === "string" ? claims.email : null };
  } catch {
    return null;
  }
}
