// View-model logic for /leagues and a league's settings page (Phase 3): what the /v2 payloads mean for the UI.
// Pure functions only (no Next, no fetch, no environment) so node --test covers them. The backend is authoritative.

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
  committedAt: string | null;         // when the served version was published
};

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
    committedAt: data ? str(data.committed_at) : null,
  };
}

export function parseMyLeagues(payload: unknown): MyLeague[] {
  const list = isRecord(payload) && Array.isArray(payload.leagues) ? payload.leagues : [];
  return list.map(parseMyLeague).filter((l): l is MyLeague => l !== null);
}

export function leagueName(l: Pick<MyLeague, "name" | "externalId">): string {
  return l.name?.trim() || `ESPN league ${l.externalId}`;
}

/** What a reader of this league sees right now, in words, and how worried to be. */
export function leagueState(l: Pick<MyLeague, "status" | "syncState" | "visibility">): { label: string; tone: Tone; detail: string } {
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
    case "first_sync_queued": return { label: "First sync queued", tone: "info", detail: "Usually ready within a few minutes." };
    case "first_sync_running": return { label: "First sync running", tone: "info", detail: "Usually ready within a few minutes." };
    case "first_sync_failed": return { label: "First sync failed", tone: "bad", detail: "It is retried automatically; you can also refresh it." };
    default: return { label: "No data yet", tone: "neutral", detail: "Refresh to start a sync." };
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
