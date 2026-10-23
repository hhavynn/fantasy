import "server-only";
import { createHash } from "node:crypto";
import { cookies, headers } from "next/headers";
import { apiRequest } from "./api";
import { EMAIL_RE, authConfig, safeNext, siteOrigin } from "./auth-config";
import {
  LINK_MESSAGES, RECENT_LINK_COOKIE, RECENT_LINK_SECONDS, isRecentLink, linkOutcome, linkState, parseSignupStatus,
  recentLinkValue, signupGate, type LinkIntent, type LinkState, type SignupGate,
} from "./auth-flow";
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
  const addressHash = createHash("sha256").update(email).digest("hex").slice(0, 32);
  if (isRecentLink(jar.get(RECENT_LINK_COOKIE)?.value, addressHash, now)) {
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
      emailRedirectTo: `${origin}/auth/callback?next=${encodeURIComponent(next)}`,
    },
  });
  const { outcome, log } = linkOutcome(error);
  if (log) console.warn(`[auth] ${intent} link request: ${log}`);
  if (outcome === "sent") {
    jar.set(RECENT_LINK_COOKIE, recentLinkValue(addressHash, now), {
      httpOnly: true, sameSite: "lax", path: "/", maxAge: RECENT_LINK_SECONDS, secure: origin.startsWith("https://"),
    });
  }
  return linkState(intent, outcome);
}
