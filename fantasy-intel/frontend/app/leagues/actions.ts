"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { apiRequest } from "@/lib/api";
import { authConfig } from "@/lib/auth-config";
import {
  cleanCookies, isLeagueRef, multileagueUiEnabled, NOT_ENABLED_REASON, parseLeagueInput, parseProviderChoice,
  parseSleeperLeagueInput, validSeason,
} from "@/lib/my-leagues";
import { currentUser } from "@/lib/supabase/server";

// Server actions are public POST endpoints: each re-checks the flag and the signed-in user on the server, and the
// backend authorizes again with the user's own token (lib/api.ts forwards it). Cookies are passed straight to the
// backend and never returned, logged or kept in state.

export type AddState = {
  status: "idle" | "ok" | "needs_cookies" | "error";
  message: string | null;
  leagueId?: string;
  league?: string;
  season?: number;
};

export type ActionResult = { ok: boolean; message: string | null };

const SIGN_IN = "Sign in to add or change leagues.";

async function gate(): Promise<string | null> {
  if (!multileagueUiEnabled(process.env.MULTILEAGUE_UI_ENABLED)) return NOT_ENABLED_REASON;
  if (!authConfig(process.env) || !(await currentUser())) return SIGN_IN;
  return null;
}

function readLeague(form: FormData): { league: string; season: number } | { error: string } {
  const parsed = parseLeagueInput(String(form.get("league") ?? ""));
  if (!parsed) return { error: "Enter the league id (the number after leagueId= in the ESPN address) or paste the address." };
  const season = parsed.season ?? validSeason(String(form.get("season") ?? ""));
  if (!season) return { error: "Enter the season as a year, for example 2026." };
  return { league: parsed.leagueId, season };
}

/** A Sleeper league: always public, so one step with no cookies. The backend's own message (503, 404, 422) is shown as-is. */
async function addSleeperLeague(form: FormData): Promise<AddState> {
  const parsed = parseSleeperLeagueInput(String(form.get("league") ?? ""));
  if (!parsed) return { status: "error", message: "Enter the Sleeper league id (the long number in the sleeper.com/leagues/… address) or paste the address." };
  const res = await apiRequest<{ league_id: string }>("/v2/leagues/import", { body: { provider: "sleeper", league: parsed.leagueId } });
  if (res.ok && res.data) {
    revalidatePath("/leagues");
    return { status: "ok", message: "Added. The first sync usually takes a few minutes.", leagueId: res.data.league_id };
  }
  return { status: "error", message: res.ok ? "The league could not be added." : res.error };
}

/** Step 1: try the league as a public one. ESPN saying "private" moves the form to step 2 (cookies). */
export async function addLeague(_prev: AddState, form: FormData): Promise<AddState> {
  const blocked = await gate();
  if (blocked) return { status: "error", message: blocked };
  if (parseProviderChoice(form.get("provider")) === "sleeper") return addSleeperLeague(form);
  const input = readLeague(form);
  if ("error" in input) return { status: "error", message: input.error };
  const res = await apiRequest<{ league_id: string }>("/v2/leagues/import", { body: input });
  if (res.ok && res.data) {
    revalidatePath("/leagues");
    return { status: "ok", message: "Added. The first sync usually takes a few minutes.", leagueId: res.data.league_id };
  }
  if (!res.ok && res.status === 409) {
    return { status: "needs_cookies", message: "This league is private on ESPN. To add it, paste your ESPN cookies below.", ...input };
  }
  return { status: "error", message: res.ok ? "The league could not be added." : res.error };
}

/** Step 2: a private league, with the visitor's own ESPN cookies and their explicit consent. */
export async function connectPrivate(_prev: AddState, form: FormData): Promise<AddState> {
  const blocked = await gate();
  if (blocked) return { status: "error", message: blocked };
  const input = readLeague(form);
  if ("error" in input) return { status: "error", message: input.error };
  if (form.get("consent") !== "on") return { status: "needs_cookies", message: "Tick the box to confirm you understand what the cookies give access to.", ...input };
  const cookies = cleanCookies(String(form.get("espn_s2") ?? ""), String(form.get("swid") ?? ""));
  if (!cookies) return { status: "needs_cookies", message: "Paste both cookies: espn_s2 (a long value) and SWID (in braces).", ...input };
  const res = await apiRequest<{ league_id: string; cookies: string }>("/v2/leagues/connect",
    { body: { ...input, ...cookies, consent: true } });
  if (res.ok && res.data) {
    revalidatePath("/leagues");
    const joined = res.data.cookies === "existing";
    return { status: "ok", leagueId: res.data.league_id,
      message: joined ? "Joined. This league already syncs with another member's cookies." : "Connected. The first sync usually takes a few minutes." };
  }
  return { status: "needs_cookies", message: res.ok ? "The league could not be connected." : res.error, ...input };
}

async function leagueCall(leagueId: string, path: string, method: "POST" | "PUT" | "DELETE", body?: unknown): Promise<ActionResult> {
  const blocked = await gate();
  if (blocked) return { ok: false, message: blocked };
  if (!isLeagueRef(leagueId)) return { ok: false, message: "Unknown league." };
  const res = await apiRequest(`/v2/leagues/${leagueId}${path}`, { method, body });
  revalidatePath("/leagues");
  revalidatePath(`/leagues/${leagueId}`);
  return res.ok ? { ok: true, message: null } : { ok: false, message: res.error };
}

export async function refreshLeague(leagueId: string): Promise<ActionResult> {
  return leagueCall(leagueId, "/sync", "POST", {});
}

export async function unpinLeague(leagueId: string): Promise<ActionResult> {
  return leagueCall(leagueId, "/pin", "DELETE");
}

export async function pinLeague(leagueId: string): Promise<ActionResult> {
  return leagueCall(leagueId, "/pin", "PUT", {});
}

export async function saveTeam(leagueId: string, _prev: ActionResult, form: FormData): Promise<ActionResult> {
  const team = String(form.get("team") ?? "");
  if (!team) return leagueCall(leagueId, "/perspective", "DELETE");
  if (!/^\d{1,9}$/.test(team)) return { ok: false, message: "Pick a team." };
  return leagueCall(leagueId, "/perspective", "PUT", { team_external_id: team });
}

export async function replaceCookies(leagueId: string, _prev: ActionResult, form: FormData): Promise<ActionResult> {
  if (form.get("consent") !== "on") return { ok: false, message: "Tick the box to confirm you understand what the cookies give access to." };
  const cookies = cleanCookies(String(form.get("espn_s2") ?? ""), String(form.get("swid") ?? ""));
  if (!cookies) return { ok: false, message: "Paste both cookies: espn_s2 (a long value) and SWID (in braces)." };
  return leagueCall(leagueId, "/credential", "PUT", { ...cookies, consent: true });
}

export async function removeCookies(leagueId: string): Promise<ActionResult> {
  return leagueCall(leagueId, "/credential", "DELETE");
}

export type InviteState = ActionResult & { link?: string };

export async function createInvite(leagueId: string, _prev: InviteState, _form: FormData): Promise<InviteState> {
  void _form;
  const blocked = await gate();
  if (blocked) return { ok: false, message: blocked };
  if (!isLeagueRef(leagueId)) return { ok: false, message: "Unknown league." };
  const res = await apiRequest<{ token: string }>(`/v2/leagues/${leagueId}/invites`, { method: "POST", body: {} });
  revalidatePath(`/leagues/${leagueId}`);
  if (!res.ok || !res.data) return { ok: false, message: res.ok ? "No invite was created." : res.error };
  // The token travels in the fragment: browsers never send it to a server, so it stays out of logs.
  return { ok: true, message: null, link: `/invite#${res.data.token}` };
}

export async function revokeInvite(leagueId: string, inviteId: string): Promise<ActionResult> {
  if (!/^[0-9a-f-]{36}$/.test(inviteId)) return { ok: false, message: "Unknown invite." };
  return leagueCall(leagueId, `/invites/${inviteId}`, "DELETE");
}

export async function deleteLeague(leagueId: string): Promise<ActionResult> {
  const result = await leagueCall(leagueId, "", "DELETE");
  if (result.ok) redirect("/leagues");
  return result;
}

export async function redeemInvite(token: string): Promise<ActionResult> {
  const blocked = await gate();
  if (blocked) return { ok: false, message: blocked };
  if (!/^[A-Za-z0-9_-]{16,64}$/.test(token)) return { ok: false, message: "This invite link is not complete." };
  const res = await apiRequest<{ league_id: string }>("/v2/invites/redeem", { body: { token } });
  if (!res.ok || !res.data) return { ok: false, message: res.ok ? "This invite is not valid." : res.error };
  redirect(`/l/${res.data.league_id}`);
}
