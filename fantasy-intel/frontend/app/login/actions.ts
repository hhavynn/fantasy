"use server";

import { headers } from "next/headers";
import { redirect } from "next/navigation";
import { EMAIL_RE, authConfig, safeNext, siteOrigin } from "@/lib/auth-config";
import { supabaseServer } from "@/lib/supabase/server";

export type LoginState = { status: "idle" | "sent" | "error"; message: string | null };

const OFF = "Sign-in is not available in this deployment.";

/**
 * Email a one-time sign-in link. Until sign-ups open (SIGNUPS_OPEN=1, Phase 6) only existing accounts get one; the
 * answer is the same either way, so the form never tells anyone whether an address has an account.
 */
export async function sendLink(_prev: LoginState, form: FormData): Promise<LoginState> {
  const config = authConfig(process.env);
  const supabase = await supabaseServer();
  if (!config || !supabase) return { status: "error", message: OFF };
  const email = String(form.get("email") ?? "").trim().toLowerCase();
  if (!EMAIL_RE.test(email)) return { status: "error", message: "Enter your email address." };
  const h = await headers();
  const next = safeNext(String(form.get("next") ?? ""));
  const origin = siteOrigin((n) => h.get(n), process.env);
  const { error } = await supabase.auth.signInWithOtp({
    email,
    options: { shouldCreateUser: config.signupsOpen, emailRedirectTo: `${origin}/auth/callback?next=${encodeURIComponent(next)}` },
  });
  if (error && error.status === 429) return { status: "error", message: "Too many sign-in emails. Try again in a little while." };
  // Any other refusal (no such account while sign-ups are closed) looks the same as success.
  return { status: "sent", message: "If this address has an account, a sign-in link is on its way. It works once, in this browser." };
}

export async function signOut(): Promise<void> {
  const supabase = await supabaseServer();
  if (supabase) await supabase.auth.signOut();
  redirect("/");
}
