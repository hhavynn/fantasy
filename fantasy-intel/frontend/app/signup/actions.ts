"use server";

import type { LinkState } from "@/lib/auth-flow";
import { requestEmailLink } from "@/lib/email-link.server";

/**
 * Sign up: email a link that creates the account when it is opened, only while both launch gates are open
 * (SIGNUPS_OPEN on this app, SIGNUPS_ENABLED on the API). Closed, nothing is sent. The answer is the same whether or not
 * the address already has an account (Supabase then sends a sign-in link instead).
 */
export async function sendSignupLink(_prev: LinkState, form: FormData): Promise<LinkState> {
  return requestEmailLink("signup", form);
}
