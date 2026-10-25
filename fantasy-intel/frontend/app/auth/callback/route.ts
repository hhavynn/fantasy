import { NextResponse, type NextRequest } from "next/server";
import { apiRequest } from "@/lib/api";
import { safeNext } from "@/lib/auth-config";
import {
  apiRefusedAccount, callbackFailureHref, callbackProblem, parseIntent, sameSiteLocation, type CallbackProblem,
} from "@/lib/auth-flow";
import { currentUser, supabaseServer } from "@/lib/supabase/server";

// The email link lands here with a one-time code (PKCE: the verifier is the cookie set when the link was requested,
// so a link only works in the browser that asked for it). The session cookies are set, then the visitor goes on.
// A link Supabase refused (expired: ?error_code=otp_expired) or a code that does not exchange (used, replaced, another
// browser) goes back, with a specific reason, to the page the visitor started from: `intent=signup` (set by the
// sign-up action, validated as an enum) means /signup, which re-checks the gates; anything else means /login.
// `next` is always an in-app path (safeNext), and every Location is same-site and never protocol-relative.
export async function GET(request: NextRequest) {
  const url = request.nextUrl;
  const code = url.searchParams.get("code");
  const next = safeNext(url.searchParams.get("next"));
  const intent = parseIntent(url.searchParams.get("intent"));
  // A relative Location: the browser resolves it against the address it used, so the visitor stays on this site and
  // on the host its new session cookies belong to (Next's own idea of the origin can be another name for the server).
  const to = (path: string) =>
    new NextResponse(null, { status: 307, headers: { Location: sameSiteLocation(path), "Cache-Control": "no-store" } });
  const fail = (problem: CallbackProblem) => to(callbackFailureHref(intent, problem, next));
  const supabase = await supabaseServer();
  if (!supabase) return fail("invalid");

  let problem: CallbackProblem;
  if (!code || url.searchParams.has("error") || url.searchParams.has("error_code")) {
    problem = callbackProblem(url.searchParams.get("error_code"));
  } else {
    const { data, error } = await supabase.auth.exchangeCodeForSession(code);
    if (!error && data.session) {
      // The API, not this app, decides whether the address is an account (fi.ensure_app_user, SIGNUPS_ENABLED). A new
      // Supabase user it refuses (sign-ups closed there, or opened here alone) would be signed in to an account that
      // can do nothing: sign it out and say why. /v2/me's only 403 is that refusal, so the status decides. Any other
      // answer, including no answer, leaves sign-in as it was; the API still refuses every write for an account it
      // has not accepted.
      const me = await apiRequest("/v2/me", { headers: { Authorization: `Bearer ${data.session.access_token}` } });
      if (apiRefusedAccount(me)) {
        await supabase.auth.signOut({ scope: "local" });
        return fail("closed");
      }
      return to(next);
    }
    problem = callbackProblem(error?.code);
  }
  // An old or reused link opened in a browser that is already signed in: nothing to fix, carry on.
  if (await currentUser()) return to(next);
  return fail(problem);
}
