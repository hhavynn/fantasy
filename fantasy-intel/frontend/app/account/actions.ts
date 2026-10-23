"use server";

import { redirect } from "next/navigation";
import { apiRequest } from "@/lib/api";
import { authConfig } from "@/lib/auth-config";
import { actionErrorMessage } from "@/lib/route-error";
import { currentUser, supabaseServer } from "@/lib/supabase/server";
import { CONFIRM_WORD } from "./confirm";

export type DeleteState = { ok: boolean; message: string | null };

/**
 * Delete the signed-in account (Phase 6). A public POST endpoint like every server action: it re-checks the user,
 * and the backend deletes only the account its verified token names. Then the browser session is signed out.
 */
export async function deleteAccount(_prev: DeleteState, form: FormData): Promise<DeleteState> {
  if (!authConfig(process.env) || !(await currentUser())) return { ok: false, message: "Sign in first." };
  if (String(form.get("confirm") ?? "").trim() !== CONFIRM_WORD) {
    return { ok: false, message: `Type ${CONFIRM_WORD} to confirm.` };
  }
  const res = await apiRequest("/v2/me", { method: "DELETE" });
  // Fixed words unless the backend wrote a 4xx message for the visitor: never the internal API_URL or a server error.
  if (!res.ok) return { ok: false, message: actionErrorMessage(res, "Your account could not be deleted right now. Try again in a few minutes.") };
  const supabase = await supabaseServer();
  if (supabase) await supabase.auth.signOut();
  redirect("/leagues");
}
