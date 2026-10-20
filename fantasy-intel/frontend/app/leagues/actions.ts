"use server";

import { NOT_ENABLED_REASON, SIGNED_OUT_REASON, multileagueUiEnabled } from "@/lib/leagues";
import type { ActionState } from "./state";

// Server actions are public POST endpoints: every one re-checks the flag and the user itself and takes
// the user from the server, never from the form. There is no sign-in yet, so every mutation is refused here
// and never forwarded; Phase 2 (Supabase Auth) forwards the verified session and Phase 3 rebuilds connect.

/** Every mutation is refused on the server until sign-in exists; nothing is forwarded to the backend. */
async function refuse(): Promise<ActionState> {
  if (!multileagueUiEnabled(process.env.MULTILEAGUE_UI_ENABLED)) return { status: "error", message: NOT_ENABLED_REASON };
  return { status: "error", message: SIGNED_OUT_REASON };
}

export async function connectLeague(_prev: ActionState, _formData: FormData): Promise<ActionState> {
  void _prev; void _formData;     // the signature useActionState calls; nothing is read until sign-in exists
  return refuse();
}

export async function setMyTeam(_leagueId: string, _prev: ActionState, _formData: FormData): Promise<ActionState> {
  void _leagueId; void _prev; void _formData;
  return refuse();
}

export async function removeLeague(_leagueId: string): Promise<ActionState> {
  void _leagueId;
  return refuse();
}
