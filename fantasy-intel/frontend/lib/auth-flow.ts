// The small pure decisions behind passwordless Sign in and Sign up (issue #270): whether sign-ups are open, what an
// email-link request's answer says, and what a failed email link means. No Next, no network: node --test covers them.
// The server actions (lib/email-link.server.ts), the pages (app/login, app/signup) and the email callback
// (app/auth/callback/route.ts) use them.
//
// Two launch gates, both required before anything can create an account:
//   - SIGNUPS_OPEN=1 on the web app (lib/auth-config.ts): whether this site offers sign-up at all;
//   - SIGNUPS_ENABLED=1 on the API, enforced in Postgres by fi.ensure_app_user and reported by GET /v2/signups.
// The API's is the one that matters (Supabase Auth can be called directly with the publishable key); the web app only
// avoids promising, or creating in Supabase Auth, an account the API would refuse.

import type { AuthConfig } from "./auth-config.ts";

export type LinkIntent = "signin" | "signup";

export type SignupGate =
  | { open: true; reason: "open" }
  /** auth_off: no sign-in at all; web_closed: SIGNUPS_OPEN unset; api_closed: SIGNUPS_ENABLED unset;
   *  api_unknown: the API did not answer (fail closed). Visitors see the same "not open yet" notice for each. */
  | { open: false; reason: "auth_off" | "web_closed" | "api_closed" | "api_unknown" };

/** Sign-up is open only when the web app offers it and the API answered that it accepts new accounts. */
export function signupGate(config: AuthConfig | null, apiOpen: boolean | null): SignupGate {
  if (!config) return { open: false, reason: "auth_off" };
  if (!config.signupsOpen) return { open: false, reason: "web_closed" };
  if (apiOpen === null) return { open: false, reason: "api_unknown" };
  return apiOpen ? { open: true, reason: "open" } : { open: false, reason: "api_closed" };
}

/** GET /v2/signups answers `{"open": true|false}`; anything else is unknown. */
export function parseSignupStatus(data: unknown): boolean | null {
  const open = (data as { open?: unknown } | null)?.open;
  return typeof open === "boolean" ? open : null;
}

export type LinkOutcome = "sent" | "rate_limited" | "invalid_email" | "unavailable";
export type OtpError = { status?: number; code?: string; name?: string } | null | undefined;

/**
 * What a Supabase signInWithOtp answer means to the visitor. Every answer that could depend on whether the address has
 * an account reads as "sent": no such account while sign-in only (otp_disabled), Supabase's own sign-up switch
 * (signup_disabled), an address the project may not email, or a failure sending the email (500). `log` names the
 * code worth an operator's attention (never the address). Only answers that are about the request itself get their
 * own message: the address's shape, a rate limit, and Supabase being unreachable.
 */
export function linkOutcome(error: OtpError): { outcome: LinkOutcome; log: string | null } {
  if (!error) return { outcome: "sent", log: null };
  const code = error.code ?? "";
  if (error.status === 429 || code === "over_email_send_rate_limit" || code === "over_request_rate_limit") {
    return { outcome: "rate_limited", log: null };
  }
  if (code === "email_address_invalid") return { outcome: "invalid_email", log: null };
  // A network failure (status 0) or a gateway error reaches every address alike.
  if (!error.status || error.status === 502 || error.status === 503 || error.status === 504) {
    return { outcome: "unavailable", log: code || error.name || `http_${error.status ?? 0}` };
  }
  // otp_disabled is the expected answer for an unknown address on Sign in: not worth a log line.
  return { outcome: "sent", log: code === "otp_disabled" ? null : code || `http_${error.status}` };
}

export type LinkState = {
  status: "idle" | "sent" | "error" | "closed";
  message: string | null;
  /** The field the error is about (the email input gets aria-invalid and points at the message). */
  field?: "email";
};

export const LINK_MESSAGES = {
  off: "Sign-in is not available in this deployment.",
  invalidEmail: "Enter a valid email address, like name@example.com.",
  recent: "A link for this address was sent less than a minute ago. Check your inbox and spam folder; that link still works. You can ask again after a minute.",
  rateLimited: "Too many link emails were asked for. Wait a few minutes, then ask for a new link.",
  unavailable: "The sign-in service could not be reached. Try again in a moment.",
  closed: "Sign-ups are not open yet. If you already have an account, sign in instead.",
  sent: {
    signin: "If this address has an account, a sign-in link is on its way. Open it in this browser within an hour; it works once.",
    signup: "If this address can be used here, a link is on its way. It finishes creating your account, or signs you in if the address already has one. Open it in this browser within an hour; it works once.",
  },
} as const;

/** The form's next state for an outcome. Same text for an address with an account and one without. */
export function linkState(intent: LinkIntent, outcome: LinkOutcome): LinkState {
  switch (outcome) {
    case "sent": return { status: "sent", message: LINK_MESSAGES.sent[intent] };
    case "rate_limited": return { status: "error", message: LINK_MESSAGES.rateLimited };
    case "invalid_email": return { status: "error", message: LINK_MESSAGES.invalidEmail, field: "email" };
    default: return { status: "error", message: LINK_MESSAGES.unavailable };
  }
}

// One link per address per browser per minute, decided before Supabase is asked, so a repeated submit gets the same
// answer whether or not the address has an account (Supabase itself throttles only addresses it has) and does not
// replace the link already on its way. The cookie holds a hash of the address, never the address.
export const RECENT_LINK_COOKIE = "fi_link_recent";
export const RECENT_LINK_SECONDS = 60;

export function recentLinkValue(addressHash: string, nowSeconds: number): string {
  return `${addressHash}.${Math.floor(nowSeconds)}`;
}

export function isRecentLink(cookie: string | null | undefined, addressHash: string, nowSeconds: number): boolean {
  const m = /^([a-f0-9]{16,64})\.(\d{1,12})$/.exec(cookie ?? "");
  if (!m || m[1] !== addressHash) return false;
  const age = nowSeconds - Number(m[2]);
  return age >= 0 && age < RECENT_LINK_SECONDS;
}

export type CallbackProblem = "expired" | "used" | "browser" | "invalid" | "closed";

/**
 * Why an email link did not sign anyone in, from Supabase's redirect (`error_code`, e.g. otp_expired) or the code
 * exchange's error code. A missing verifier cookie means another browser, or a link already used here (the exchange
 * removes the verifier), so its message names both.
 */
export function callbackProblem(errorCode: string | null | undefined): CallbackProblem {
  switch (errorCode) {
    case "otp_expired":
    case "flow_state_expired":
      return "expired";
    case "flow_state_not_found":
    case "bad_code_verifier":
      return "used";
    case "pkce_code_verifier_not_found":
      return "browser";
    default:
      return "invalid";
  }
}

/** The `?error=` value on /login and /signup (`1` is the callback's older generic value). */
export function parseCallbackProblem(value: string | string[] | undefined): CallbackProblem | null {
  const v = Array.isArray(value) ? value[0] : value;
  if (!v) return null;
  return v === "expired" || v === "used" || v === "browser" || v === "closed" ? v : "invalid";
}

export const CALLBACK_PROBLEMS: Record<CallbackProblem, { title: string; body: string }> = {
  // Supabase answers otp_expired for a link opened a second time as well as for an old one.
  expired: {
    title: "That link has expired or was already used",
    body: "Each link works once, for one hour. Enter your email below for a new one.",
  },
  used: {
    title: "That link was already used or replaced",
    // Supabase keeps one pending link per browser, and a failed attempt also cancels the pending one.
    body: "Each link works once, and opening an older link cancels the others. Ask for a new link below and open only that email.",
  },
  browser: {
    title: "Open the link in the browser that asked for it",
    body: "A link works once, and only in the browser where you entered your email. Ask for a new link here, then open it in this browser.",
  },
  invalid: { title: "That link did not work", body: "It may be incomplete or out of date. Ask for a new link below." },
  closed: {
    title: "Sign-ups are not open yet",
    body: "Your email address was confirmed, but Fantasy Intel is not accepting new accounts yet, so you were signed out and nothing was saved. If you already have an account, sign in with its address.",
  },
};

/** /login or /signup, carrying an explicitly chosen in-app destination (already passed through safeNext). */
export function authHref(page: "/login" | "/signup", next: string): string {
  return next === "/leagues" ? page : `${page}?next=${encodeURIComponent(next)}`;
}
