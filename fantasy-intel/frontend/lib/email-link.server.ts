import "server-only";
import { cookies, headers } from "next/headers";
import { apiRequest } from "./api";
import { EMAIL_RE, authConfig, safeNext, siteOrigin } from "./auth-config";
import {
  LINK_MESSAGES, RECENT_LINK_COOKIE, RECENT_LINK_SECONDS, isRecentLink, linkOutcome, linkState, parseSignupStatus,
  recentLinkValue, signupGate, type LinkIntent, type LinkState, type SignupGate,
} from "./auth-flow";
import { linkGuardHash, linkGuardSecret } from "./link-guard";
import { supabaseServer } from "./supabase/server";

// The one email-link request behind both Sign in and Sign up (the existing Supabase OTP + PKCE flow; no second auth
// stack). Sign in never creates an account. Sign up asks Supabase to create one only while both launch gates are open
// (lib/auth-flow.ts); the API's fi.ensure_app_user still decides when the link is opened (app/auth/callback/route.ts).

/** Both launch gates, asked fresh per request. The API's answer is required: no answer means closed. */
export async function signupGateNow(): Promise<SignupGate> {
  const config = authConfig(process.env);
  if (!config?.signupsOpen) return signupGate(config, null);
  const res = await apiRequest("/v2/signups");
  return signupGate(config, res.ok ? parseSignupStatus(res.data) : null);
}

export async function requestEmailLink(intent: LinkIntent, form: FormData): Promise<LinkState> {
  const config = authConfig(process.env);
  const supabase = await supabaseServer();
  if (!config || !supabase) return { status: "error", message: LINK_MESSAGES.off };
  const email = String(form.get("email") ?? "").trim().toLowerCase();
  if (!EMAIL_RE.test(email)) return { status: "error", message: LINK_MESSAGES.invalidEmail, field: "email" };
  if (intent === "signup" && !(await signupGateNow()).open) return { status: "closed", message: LINK_MESSAGES.closed };

  const jar = await cookies();
  const now = Date.now() / 1000;
  // The repeat guard needs a server-side key (lib/link-guard.ts); without one it is off, never an unkeyed hash.
  const guardKey = linkGuardSecret(process.env);
  const addressHash = guardKey ? linkGuardHash(email, guardKey) : null;
  if (addressHash && isRecentLink(jar.get(RECENT_LINK_COOKIE)?.value, addressHash, now)) {
    return { status: "error", message: LINK_MESSAGES.recent };
  }

  const h = await headers();
  const next = safeNext(String(form.get("next") ?? ""));
  const origin = siteOrigin((n) => h.get(n), process.env);
  const { error } = await supabase.auth.signInWithOtp({
    email,
    options: {
      // Sign in is sign-in only, even while sign-ups are open: a new address is never registered from it.
      shouldCreateUser: intent === "signup",
      // `intent` only chooses which page a failed link returns to (lib/auth-flow.ts parseIntent); nothing else reads it.
      emailRedirectTo: `${origin}/auth/callback?next=${encodeURIComponent(next)}${intent === "signup" ? "&intent=signup" : ""}`,
    },
  });
  const { outcome, log } = linkOutcome(error);
  if (log === "signup_disabled") {
    console.warn("[auth] signup link request: signup_disabled. SIGNUPS_OPEN and the API's SIGNUPS_ENABLED are on but "
      + "Supabase Auth's \"Allow new users to sign up\" is off: a misconfiguration (the visitor was told a link may be "
      + "on its way, so the answer does not reveal whether the address has an account).");
  } else if (log) {
    console.warn(`[auth] ${intent} link request: ${log}`);
  }
  if (outcome === "sent" && addressHash) {
    jar.set(RECENT_LINK_COOKIE, recentLinkValue(addressHash, now), {
      httpOnly: true, sameSite: "lax", path: "/", maxAge: RECENT_LINK_SECONDS, secure: origin.startsWith("https://"),
    });
  }
  return linkState(intent, outcome);
}
