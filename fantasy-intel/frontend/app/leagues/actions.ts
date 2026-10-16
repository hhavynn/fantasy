"use server";

import { revalidatePath } from "next/cache";
import { apiRequest, type ApiResult } from "@/lib/api";
import {
  NO_SIGNING_REASON, NOT_ENABLED_REASON, SIGNED_OUT_REASON, isInternalLeagueId, isProviderId, multileagueUiEnabled,
  parseLeague, validTeamId, validateConnect,
} from "@/lib/leagues";
import { currentUserId, userAssertionHeaders } from "@/lib/user-assertion.server";
import type { ActionState } from "./state";

// Server actions are public POST endpoints: every one re-checks the flag and the user itself and takes
// the user from the server, never from the form. The assertion header is built here and sent straight
// to the backend; it is never returned to the browser.

type Guard = { ok: true; headers: Record<string, string> } | { ok: false; state: ActionState };

async function guard(): Promise<Guard> {
  if (!multileagueUiEnabled(process.env.MULTILEAGUE_UI_ENABLED)) return fail(NOT_ENABLED_REASON);
  const userId = await currentUserId();
  if (!userId) return fail(SIGNED_OUT_REASON);
  const headers = userAssertionHeaders(userId);
  if (!headers) return fail(NO_SIGNING_REASON);
  return { ok: true, headers };
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
    headers: g.headers,
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
    headers: g.headers,
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

  const res = await apiRequest(`/v2/leagues/${encodeURIComponent(leagueId)}`, { method: "DELETE", headers: g.headers });
  if (!res.ok) return { status: "error", message: backendMessage(res) };
  revalidatePath("/leagues");
  return { status: "ok", message: "Connection removed." };
}
