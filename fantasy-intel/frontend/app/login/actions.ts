"use server";

import { redirect } from "next/navigation";
import type { LinkState } from "@/lib/auth-flow";
import { requestEmailLink } from "@/lib/email-link.server";
import { supabaseServer } from "@/lib/supabase/server";

export type LoginState = LinkState;

/**
 * Sign in: email a one-time link to an existing account. Never creates one, even while sign-ups are open (Sign up is
 * app/signup/actions.ts). The answer is the same whether or not the address has an account.
 */
export async function sendLink(_prev: LinkState, form: FormData): Promise<LinkState> {
  return requestEmailLink("signin", form);
}

export async function signOut(): Promise<void> {
  const supabase = await supabaseServer();
  if (supabase) await supabase.auth.signOut();
  redirect("/");
}
