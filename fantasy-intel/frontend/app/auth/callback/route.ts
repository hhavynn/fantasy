import { NextResponse, type NextRequest } from "next/server";
import { safeNext } from "@/lib/auth-config";
import { supabaseServer } from "@/lib/supabase/server";

// The email link lands here with a one-time code (PKCE: the verifier is the cookie set when the link was requested,
// so a link only works in the browser that asked for it). The session cookies are set, then the visitor goes on.
export async function GET(request: NextRequest) {
  const url = request.nextUrl;
  const code = url.searchParams.get("code");
  const next = safeNext(url.searchParams.get("next"));
  const supabase = await supabaseServer();
  if (supabase && code) {
    const { error } = await supabase.auth.exchangeCodeForSession(code);
    if (!error) return NextResponse.redirect(new URL(next, url.origin));
  }
  return NextResponse.redirect(new URL(`/login?error=1&next=${encodeURIComponent(next)}`, url.origin));
}
