// View-model logic for /leagues and a league's settings page (Phase 3): what the /v2 payloads mean for the UI.
// Pure functions only (no Next, no fetch, no environment) so node --test covers them. The backend is authoritative.

import { normalizeProvider, providerName, unnamedLeague } from "./labels.ts";

export const NOT_ENABLED_REASON = "Multi-league support is not enabled in this deployment.";

/** The feature flag is exactly "1": anything else (unset, "0", "true") is off. */
export function multileagueUiEnabled(value: string | undefined): boolean {
  return value === "1";
}

export type Tone = "good" | "warn" | "bad" | "info" | "neutral";

/** One league as GET /v2/leagues and GET /v2/leagues/{id} describe it (backend authz.Access.to_json). */
export type MyLeague = {
  id: string;
  provider: string;
  externalId: string;
  season: number;
  name: string | null;
  visibility: "public" | "private";
  status: string;                     // active | stale | unavailable | dormant
  role: "admin" | "viewer" | null;
  pinned: boolean;
  teamId: string | null;              // the saved perspective
  syncState: string;                  // ready | first_sync_queued | first_sync_running | first_sync_failed | no_data
  /** Why the latest sync job is retrying or gave up (backend authz._sync_issue), or null when nothing went wrong. */
  syncIssue: SyncIssue | null;
  committedAt: string | null;         // when the served version was published
};

export type SyncIssue = "provider_unreachable" | "access" | "not_found" | "internal";
const SYNC_ISSUES: readonly string[] = ["provider_unreachable", "access", "not_found", "internal"];

/** The backend's category; a value this client does not know reads as "internal" (something went wrong). */
export function parseSyncIssue(value: unknown): SyncIssue | null {
  if (typeof value !== "string" || !value) return null;
  return (SYNC_ISSUES.includes(value) ? value : "internal") as SyncIssue;
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const str = (v: unknown): string | null => (typeof v === "string" && v.trim() ? v : null);

export function parseMyLeague(raw: unknown): MyLeague | null {
  if (!isRecord(raw)) return null;
  const { league_id, provider, external_league_id, season } = raw;
  if (typeof league_id !== "string" || typeof provider !== "string" || typeof external_league_id !== "string"
      || typeof season !== "number") return null;
  const data = isRecord(raw.data) ? raw.data : null;
  return {
    id: league_id, provider, externalId: external_league_id, season,
    name: str(raw.name),
    visibility: raw.visibility === "private" ? "private" : "public",
    status: str(raw.status) ?? "active",
    role: raw.role === "admin" || raw.role === "viewer" ? raw.role : null,
    pinned: raw.pinned === true,
    teamId: str(raw.perspective_team_id),
    syncState: str(raw.sync_state) ?? "no_data",
    syncIssue: parseSyncIssue(raw.sync_issue),
    committedAt: data ? str(data.committed_at) : null,
  };
}

export function parseMyLeagues(payload: unknown): MyLeague[] {
  const list = isRecord(payload) && Array.isArray(payload.leagues) ? payload.leagues : [];
  return list.map(parseMyLeague).filter((l): l is MyLeague => l !== null);
}

/** The league's stored name, or "ESPN league 123" / "Sleeper league 123" when it has none. */
export function leagueName(l: Pick<MyLeague, "name" | "externalId"> & { provider?: string }): string {
  return l.name?.trim() || unnamedLeague(l.provider, l.externalId);
}

/** What a reader of this league sees right now, in words, and how worried to be. */
export function leagueState(l: Pick<MyLeague, "status" | "syncState" | "visibility"> & { provider?: string; syncIssue?: SyncIssue | null }): { label: string; tone: Tone; detail: string } {
  // Sleeper leagues are always public and need no cookies: a pause or a missing league is Sleeper's answer, not a credential.
  if (normalizeProvider(l.provider) === "sleeper" && (l.status === "stale" || l.status === "unavailable")) {
    const name = providerName(l.provider);
    return l.status === "stale"
      ? { label: "Sync paused", tone: "warn", detail: `${name} stopped answering for this league. The last data stays readable; syncing is retried automatically.` }
      : { label: `Unavailable on ${name}`, tone: "bad", detail: `${name} no longer finds this league. The last data is kept for now.` };
  }
  if (l.status === "stale") {
    return { label: "Sync paused", tone: "warn",
      detail: "The ESPN cookies for this league stopped working. The last data stays readable; a member can store fresh cookies in Settings." };
  }
  if (l.status === "unavailable") {
    return { label: "Unavailable on ESPN", tone: "bad",
      detail: l.visibility === "private"
        ? "ESPN no longer shows this league publicly. A member can connect it privately with their cookies."
        : "ESPN no longer finds this league. The last data is kept for now." };
  }
  if (l.status === "dormant") {
    return { label: "Paused (unused)", tone: "neutral", detail: "Nobody opened this league for 30 days. Opening it resumes syncing." };
  }
  switch (l.syncState) {
    case "ready": return { label: "Up to date", tone: "good", detail: "Syncs every few hours, hourly on game days." };
    case "first_sync_queued":
      return l.syncIssue
        ? { label: "Retrying first sync", tone: "warn", detail: `${issueSentence(l.syncIssue, providerName(l.provider))} It is retried automatically.` }
        : { label: "First sync queued", tone: "info", detail: "Usually ready within a few minutes." };
    case "first_sync_running": return { label: "First sync running", tone: "info", detail: "Usually ready within a few minutes." };
    case "first_sync_failed":
      return { label: "First sync failed", tone: "bad",
        detail: `${l.syncIssue ? `${issueSentence(l.syncIssue, providerName(l.provider))} ` : ""}It stopped after repeated tries; refresh to try again.` };
    default: return { label: "No data yet", tone: "neutral", detail: "Refresh to start a sync." };
  }
}

/** One plain sentence for why a sync job is retrying or stopped. */
export function issueSentence(issue: SyncIssue, provider: string): string {
  switch (issue) {
    case "provider_unreachable": return `${provider} did not answer.`;
    case "access": return `${provider} refused access to this league.`;
    case "not_found": return `${provider} could not find this league.`;
    default: return "Something went wrong on our side while building the league.";
  }
}

// ---- forms ----------------------------------------------------------------------------------------

export const MIN_SEASON = 1990;
export const MAX_SEASON = 2100;

/** The NFL season a fantasy league plays in: before August it is still last year's. */
export function defaultSeason(now: Date): number {
  return now.getUTCMonth() < 7 ? now.getUTCFullYear() - 1 : now.getUTCFullYear();
}

/** An ESPN league id, or the league page's URL (…leagueId=123…), and its season when the URL names one. */
export function parseLeagueInput(text: string): { leagueId: string; season: number | null } | null {
  const value = text.trim();
  if (/^\d{1,12}$/.test(value)) return { leagueId: value, season: null };
  const id = /[?&]leagueId=(\d{1,12})/.exec(value);
  if (!id) return null;
  const season = /[?&]seasonId=(\d{4})/.exec(value);
  return { leagueId: id[1], season: season ? Number(season[1]) : null };
}

/** A Sleeper league id (6 to 20 digits, usually 18), or a sleeper.com/leagues/<id>/… address. Null otherwise. */
export function parseSleeperLeagueInput(text: string): { leagueId: string } | null {
  const value = text.trim();
  if (/^\d{6,20}$/.test(value)) return { leagueId: value };
  const m = /^(?:https?:\/\/)?(?:www\.)?sleeper\.(?:com|app)\/leagues\/(\d{6,20})(?:[/?#].*)?$/i.exec(value);
  return m ? { leagueId: m[1] } : null;
}

export type LeagueProviderChoice = "espn" | "sleeper";

/** The provider picked on the add-league form; anything but "sleeper" is ESPN (the original form). */
export function parseProviderChoice(value: unknown): LeagueProviderChoice {
  return value === "sleeper" ? "sleeper" : "espn";
}

export function validSeason(value: string): number | null {
  const n = Number(value);
  return Number.isInteger(n) && n >= MIN_SEASON && n <= MAX_SEASON ? n : null;
}

export const CONSENT_TEXT = "I understand these cookies give access to my ESPN account. They are stored encrypted, used only to read this league, and stop working when I sign out of ESPN everywhere.";

/** espn_s2 and SWID as pasted from the browser: trimmed, SWID braced. Null when either is missing or malformed. */
export function cleanCookies(s2: string, swid: string): { espn_s2: string; swid: string } | null {
  const a = s2.trim();
  const b = swid.trim().replace(/^\{?/, "{").replace(/\}?$/, "}");
  if (!/^[A-Za-z0-9%+/=._-]{20,2000}$/.test(a) || !/^\{[0-9A-Fa-f-]{36}\}$/.test(b)) return null;
  return { espn_s2: a, swid: b };
}

export function isLeagueRef(value: unknown): value is string {
  return typeof value === "string" && /^lg_[A-Za-z0-9_-]{16,64}$/.test(value);
}
