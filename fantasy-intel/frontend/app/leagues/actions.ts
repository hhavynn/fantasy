"use server";

import { revalidatePath } from "next/cache";
import { apiRequest, type ApiResult } from "@/lib/api";
import {
  NOT_ENABLED_REASON, SIGNED_OUT_REASON, isInternalLeagueId, isProviderId, multileagueUiEnabled,
  parseLeague, validTeamId, validateConnect,
} from "@/lib/leagues";
import type { ActionState } from "./state";

// Server actions are public POST endpoints: every one re-checks the flag and the user itself and takes
// the user from the server, never from the form. There is no sign-in yet, so every mutation is refused here
// and never forwarded; Phase 2 (Supabase Auth) forwards the verified session and Phase 3 rebuilds connect.

type Guard = { ok: true } | { ok: false; state: ActionState };

async function guard(): Promise<Guard> {
  if (!multileagueUiEnabled(process.env.MULTILEAGUE_UI_ENABLED)) return fail(NOT_ENABLED_REASON);
  return fail(SIGNED_OUT_REASON);
}

function fail(message: string): { ok: false; state: ActionState } {
  return { ok: false, state: { status: "error", message } };
}

/** The backend's own `detail`; an unreachable backend is described without its address. */
function backendMessage(res: Extract<ApiResult<unknown>, { ok: false }>): string {
  return res.kind === "offline" ? "The backend did not answer." : res.error;
}

export async function connectLeague(_prev: ActionState, formData: FormData): Promise<ActionState> {
  const g = await guard();
  if (!g.ok) return g.state;
  const provider = formData.get("provider");
  if (!isProviderId(provider)) return { status: "error", message: "Choose a provider." };
  const checked = validateConnect({
    leagueId: String(formData.get("external_league_id") ?? ""),
    season: String(formData.get("season") ?? ""),
  });
  if (!checked.ok) return { status: "error", message: "Check the highlighted fields.", fieldErrors: checked.errors };

  const res = await apiRequest("/v2/leagues", {
    method: "POST",
    body: { provider, external_league_id: checked.leagueId, season: checked.season },
  });
  if (!res.ok) return { status: "error", message: backendMessage(res) };
  revalidatePath("/leagues");
  const name = parseLeague(res.data)?.name;
  return { status: "ok", message: name ? `Connected ${name}.` : "League connected." };
}

export async function setMyTeam(leagueId: string, _prev: ActionState, formData: FormData): Promise<ActionState> {
  const g = await guard();
  if (!g.ok) return g.state;
  if (!isInternalLeagueId(leagueId)) return { status: "error", message: "Unknown league." };
  const teamId = formData.get("external_team_id");
  if (!validTeamId(teamId)) return { status: "error", message: "Choose a team." };

  const res = await apiRequest(`/v2/leagues/${encodeURIComponent(leagueId)}/my-team`, {
    method: "PUT",
    body: { external_team_id: teamId },
  });
  if (!res.ok) return { status: "error", message: backendMessage(res) };
  revalidatePath("/leagues");
  return { status: "ok", message: "Saved as your team." };
}

export async function removeLeague(leagueId: string): Promise<ActionState> {
  const g = await guard();
  if (!g.ok) return g.state;
  if (!isInternalLeagueId(leagueId)) return { status: "error", message: "Unknown league." };

  const res = await apiRequest(`/v2/leagues/${encodeURIComponent(leagueId)}`, { method: "DELETE" });
  if (!res.ok) return { status: "error", message: backendMessage(res) };
  revalidatePath("/leagues");
  return { status: "ok", message: "Connection removed." };
}
