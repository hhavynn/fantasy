// Cross-league player exposure (issue #209) on /leagues/portfolio. The backend (GET /v2/portfolio,
// backend/app/api/portfolio.py) enumerates and authorizes the signed-in manager's own leagues, reads each league's saved
// team from its own files and returns exact counts: per player, the leagues and teams he is on, his bye week and each
// league's data; per week, the byes and injury designations. This module only parses that payload, searches, filters
// and sorts it and words its states. It computes no score and never turns a missing value into 0. Pure, so node --test
// covers it.

import { isLeagueId, leaguePath } from "./league-scope.ts";
import { parseMyLeague, type MyLeague } from "./my-leagues.ts";
import { progressHref } from "./onboarding.ts";
import type { ProvenancePayload } from "./signal-provenance.ts";
import type { SearchParams, SortState } from "./sort.ts";

export const PORTFOLIO_CONTRACT = "portfolio.v1";
export const PORTFOLIO_PATH = "/leagues/portfolio";
/** The action center (issue #166), where this week's lineup decisions across leagues live. */
export const ACTION_CENTER_HREF = "/leagues#action-center";

export type PortfolioState = "ready" | "not_ready" | "needs_team" | "unavailable";
export type IdentityStatus = "verified" | "team_defense" | "espn_id" | "unverified" | "conflict";

export type PortfolioLeague = {
  league: MyLeague;
  state: PortfolioState;
  reason: string | null;
  /** Read from the league's previous files: the data may be older than its latest sync. */
  stale: boolean;
  team: { team_id: number; name: string | null } | null;
  week: number | null;
  finalWeek: number | null;
  syncedAt: string | null;
  rosterSize: number | null;
  projection: { source: string | null; available: boolean; reason: string | null; known: number; total: number } | null;
  provenance: ProvenancePayload | null;
};

export type Exposure = {
  leagueId: string;
  provider: string;
  leagueName: string | null;
  teamId: number;
  teamName: string | null;
  playerId: number;
  slot: string | null;
  isStarter: boolean;
  reserve: boolean;
  injuryStatus: string | null;
  projection: number | null;
  projectionOrigin: "provider" | "model" | null;
  week: number | null;
  byeWeek: number | null;
  syncedAt: string | null;
  stale: boolean;
  identity: Identity;
};

export type Identity = { status: IdentityStatus; gsisId: string | null; reason: string | null };

export type PortfolioPlayer = {
  key: string;
  name: string | null;
  position: string | null;
  nflTeam: string | null;
  identity: Identity;
  leagues: number;
  of: number;
  teams: number;
  starters: number;
  byeWeek: number | null;
  byeReason: string | null;
  injury: { designated: number; statuses: string[] };
  projections: { known: number; missing: number };
  exposures: Exposure[];
};

export type PortfolioWeek = {
  week: number;
  leaguesPlaying: number;
  bye: { exposures: number; players: number; leagues: number; starters: number; unknown: number; names: string[] };
  injuries: { exposures: number; players: number; leagues: number; leaguesReporting: number } | null;
};

export type Excluded = { league: MyLeague; reason: "other_season" | "over_limit" };

export type Portfolio = {
  generatedAt: number | null;
  limit: number;
  season: number | null;
  leagues: PortfolioLeague[];
  excluded: Excluded[];
  players: PortfolioPlayer[];
  weeks: PortfolioWeek[];
  coverage: { leaguesReady: number; leaguesTotal: number; exposures: number; players: number;
              projectionsKnown: number; projectionsMissing: number; byeUnknown: number };
};

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const int = (v: unknown, fallback = 0): number => (typeof v === "number" && Number.isInteger(v) ? v : fallback);
const str = (v: unknown): string | null => (typeof v === "string" && v ? v : null);
const STATES: readonly PortfolioState[] = ["ready", "not_ready", "needs_team", "unavailable"];
const IDENTITIES: readonly IdentityStatus[] = ["verified", "team_defense", "espn_id", "unverified", "conflict"];

function parseIdentity(raw: unknown): Identity {
  const r = isRecord(raw) ? raw : {};
  const status = IDENTITIES.includes(r.status as IdentityStatus) ? (r.status as IdentityStatus) : "unverified";
  return { status, gsisId: str(r.gsis_id), reason: str(r.reason) };
}

function parseLeague(raw: unknown): PortfolioLeague | null {
  if (!isRecord(raw)) return null;
  const league = parseMyLeague(raw.league);
  if (!league) return null;
  const state = STATES.includes(raw.state as PortfolioState) ? (raw.state as PortfolioState) : "unavailable";
  const team = isRecord(raw.team) && typeof raw.team.team_id === "number"
    ? { team_id: raw.team.team_id, name: str(raw.team.name) } : null;
  const p = isRecord(raw.projection) ? raw.projection : null;
  return {
    league, state, reason: str(raw.reason),
    // Anything but an explicit false is not fresh: an older or partial answer never reads as current.
    stale: raw.stale !== false,
    team, week: num(raw.week), finalWeek: num(raw.final_week), syncedAt: str(raw.synced_at), rosterSize: num(raw.roster_size),
    projection: p ? { source: str(p.source), available: p.available === true, reason: str(p.reason), known: int(p.known), total: int(p.total) } : null,
    provenance: isRecord(raw.provenance) ? (raw.provenance as unknown as ProvenancePayload) : null,
  };
}

function parseExposure(raw: unknown): Exposure | null {
  // The league id goes into links (leaguePath): anything that is not a league id is dropped, never linked.
  if (!isRecord(raw) || !isLeagueId(raw.league_id) || typeof raw.player_id !== "number") return null;
  const origin = raw.projection_origin === "provider" || raw.projection_origin === "model" ? raw.projection_origin : null;
  return {
    leagueId: raw.league_id, provider: str(raw.provider) ?? "espn", leagueName: str(raw.league_name),
    teamId: int(raw.team_id), teamName: str(raw.team_name), playerId: raw.player_id, slot: str(raw.slot),
    isStarter: raw.is_starter === true, reserve: raw.reserve === true, injuryStatus: str(raw.injury_status),
    projection: num(raw.projection), projectionOrigin: num(raw.projection) === null ? null : origin,
    week: num(raw.week), byeWeek: num(raw.bye_week), syncedAt: str(raw.synced_at), stale: raw.stale !== false,
    identity: parseIdentity(raw.identity),
  };
}

function parsePlayer(raw: unknown): PortfolioPlayer | null {
  if (!isRecord(raw) || typeof raw.key !== "string" || !Array.isArray(raw.exposures)) return null;
  const exposures = raw.exposures.map(parseExposure).filter((e): e is Exposure => e !== null);
  const injury = isRecord(raw.injury) ? raw.injury : {};
  const projections = isRecord(raw.projections) ? raw.projections : {};
  return {
    key: raw.key, name: str(raw.name), position: str(raw.position), nflTeam: str(raw.nfl_team),
    identity: parseIdentity(raw.identity),
    leagues: int(raw.leagues), of: int(raw.of), teams: int(raw.teams), starters: int(raw.starters),
    byeWeek: num(raw.bye_week), byeReason: str(raw.bye_reason),
    injury: { designated: int(injury.designated), statuses: Array.isArray(injury.statuses) ? injury.statuses.filter((s): s is string => typeof s === "string") : [] },
    projections: { known: int(projections.known), missing: int(projections.missing) },
    exposures,
  };
}

function parseWeek(raw: unknown): PortfolioWeek | null {
  if (!isRecord(raw) || typeof raw.week !== "number" || !isRecord(raw.bye)) return null;
  const b = raw.bye;
  const i = isRecord(raw.injuries) ? raw.injuries : null;
  return {
    week: raw.week, leaguesPlaying: int(raw.leagues_playing),
    bye: { exposures: int(b.exposures), players: int(b.players), leagues: int(b.leagues), starters: int(b.starters), unknown: int(b.unknown),
           names: Array.isArray(b.names) ? b.names.filter((n): n is string => typeof n === "string") : [] },
    injuries: i ? { exposures: int(i.exposures), players: int(i.players), leagues: int(i.leagues), leaguesReporting: int(i.leagues_reporting) } : null,
  };
}

/** The payload, or null when it is not a portfolio.v1 answer (an older or a different backend). */
export function parsePortfolio(raw: unknown): Portfolio | null {
  if (!isRecord(raw) || raw.contract !== PORTFOLIO_CONTRACT || !Array.isArray(raw.leagues) || !Array.isArray(raw.players)) return null;
  const excludedRaw = isRecord(raw.excluded) && Array.isArray(raw.excluded.leagues) ? raw.excluded.leagues : [];
  const cov = isRecord(raw.coverage) ? raw.coverage : {};
  const generated = typeof raw.generated_at === "string" ? Date.parse(raw.generated_at) : NaN;
  return {
    generatedAt: Number.isNaN(generated) ? null : generated,
    limit: int(raw.limit),
    season: num(raw.season),
    leagues: raw.leagues.map(parseLeague).filter((l): l is PortfolioLeague => l !== null),
    excluded: excludedRaw.flatMap((x) => {
      const league = isRecord(x) ? parseMyLeague(x.league) : null;
      const reason = isRecord(x) && x.reason === "other_season" ? "other_season" : "over_limit";
      return league ? [{ league, reason } as Excluded] : [];
    }),
    players: raw.players.map(parsePlayer).filter((p): p is PortfolioPlayer => p !== null),
    weeks: Array.isArray(raw.weeks) ? raw.weeks.map(parseWeek).filter((w): w is PortfolioWeek => w !== null) : [],
    coverage: {
      leaguesReady: int(cov.leagues_ready), leaguesTotal: int(cov.leagues_total), exposures: int(cov.exposures),
      players: int(cov.players), projectionsKnown: int(cov.projections_known), projectionsMissing: int(cov.projections_missing),
      byeUnknown: int(cov.bye_unknown),
    },
  };
}

// ---- words ------------------------------------------------------------------------------------------------------------

export const IDENTITY_TEXT: Record<IdentityStatus, { label: string; tone: string; detail: string }> = {
  verified: { label: "NFL id", tone: "", detail: "Joined across leagues by a verified NFL (GSIS) id." },
  team_defense: { label: "Team D/ST", tone: "", detail: "A team defense, joined by its NFL team." },
  espn_id: { label: "ESPN id", tone: "", detail: "Joined across your ESPN leagues by ESPN's player id; not linked to NFL data." },
  unverified: { label: "Not linked", tone: "warn", detail: "No id-based link to NFL data, so this row counts one league only." },
  conflict: { label: "Id conflict", tone: "warn", detail: "The id sources disagree, so no NFL link is used and nothing is joined on it." },
};

export const EXCLUDED_TEXT: Record<Excluded["reason"], string> = {
  other_season: "Another season: only your newest season's leagues are combined.",
  over_limit: "Past the limit of leagues read at once.",
};

const STATE_TEXT: Record<PortfolioState, { label: string; tone: string }> = {
  ready: { label: "Included", tone: "" },
  not_ready: { label: "Not ready", tone: "warn" },
  needs_team: { label: "No team saved", tone: "warn" },
  unavailable: { label: "Unavailable", tone: "bad" },
};

export function leagueStatus(l: PortfolioLeague): { label: string; tone: string; detail: string } {
  if (l.state === "ready" && l.stale) {
    return { label: "Not fresh", tone: "warn", detail: "Read from this league's previous files: a newer sync could not be loaded in time." };
  }
  const s = STATE_TEXT[l.state];
  return { ...s, detail: l.reason ?? (l.state === "ready" ? "Counted in every number on this page." : "Not counted on this page.") };
}

/** "Out" for OUT, "Injured reserve" for INJURY_RESERVE: the provider's designation in words. */
export function injuryWord(status: string): string {
  const s = status.toUpperCase();
  const known: Record<string, string> = {
    OUT: "Out", DOUBTFUL: "Doubtful", QUESTIONABLE: "Questionable", INJURY_RESERVE: "Injured reserve", IR: "Injured reserve",
    SUSPENSION: "Suspended", DAY_TO_DAY: "Day to day", PUP: "PUP list",
  };
  return known[s] ?? s.charAt(0) + s.slice(1).toLowerCase().replace(/_/g, " ");
}

export function portfolioProblem(res: { status?: number; error: string }): { tone: "caution" | "negative"; title: string; body: string } {
  if (res.status === 401) return { tone: "caution", title: "Sign in again to see your players", body: "Your session has ended." };
  if (res.status === 503) {
    return { tone: "caution", title: "Your leagues could not be read just now",
             body: "The server is busy reading leagues for other requests. Try again in a few seconds." };
  }
  return { tone: "negative", title: "Your player exposure could not be loaded",
           body: "The backend did not answer. Each league's own pages still work." };
}

// ---- links ----------------------------------------------------------------------------------------------------------

/** The player's page in that league (league-scoped, so the click stays in that league). */
export function exposureHref(e: Pick<Exposure, "leagueId" | "playerId">): string {
  return leaguePath(e.leagueId, `/players?id=${e.playerId}`);
}

/** The league's Overview, or its sync progress on the connections page while it has no data. */
export function myLeagueHref(l: Pick<MyLeague, "id" | "syncState">): string {
  return l.syncState === "ready" ? leaguePath(l.id, "/") : progressHref(l.id);
}

export function leagueHref(l: PortfolioLeague): string {
  return myLeagueHref(l.league);
}

// ---- search, filter and sort ---------------------------------------------------------------------------------------------

export const SORT_KEYS = ["leagues", "name", "pos", "starters", "bye", "injury", "projections"] as const;
export const DEFAULT_SORT: SortState = { key: "leagues", dir: "desc" };
export const POSITIONS = ["QB", "RB", "WR", "TE", "K", "D/ST"] as const;
export const MAX_QUERY = 60;

const first = (v: string | string[] | undefined): string | undefined => (Array.isArray(v) ? v[0] : v);

export type PortfolioFilters = { q: string; pos: string | null; shared: boolean };

export function parseFilters(sp: SearchParams | undefined): PortfolioFilters {
  const q = (first(sp?.q) ?? "").trim().slice(0, MAX_QUERY);
  const posRaw = first(sp?.pos) ?? null;
  const pos = posRaw && (POSITIONS as readonly string[]).includes(posRaw) ? posRaw : null;
  return { q, pos, shared: first(sp?.shared) === "1" };
}

const fold = (s: string) => s.normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase();

/** Rows matching the search (player name, NFL team or a league or team name), the position and "on two or more". */
export function filterPlayers(players: readonly PortfolioPlayer[], f: PortfolioFilters): PortfolioPlayer[] {
  const needle = fold(f.q);
  return players.filter((p) => {
    if (f.pos && p.position !== f.pos) return false;
    if (f.shared && p.leagues < 2) return false;
    if (!needle) return true;
    const hay = [p.name, p.nflTeam, ...p.exposures.flatMap((e) => [e.leagueName, e.teamName])].filter(Boolean).join(" ");
    return fold(hay).includes(needle);
  });
}

/** The value a sort key reads; null sorts last in both directions (lib/sort.ts): a missing bye is not week 0. */
export function sortValue(p: PortfolioPlayer, key: string): number | string | null {
  switch (key) {
    case "name": return p.name ?? null;
    case "pos": return p.position ?? null;
    case "starters": return p.starters;
    case "bye": return p.byeWeek;
    case "injury": return p.injury.designated;
    case "projections": return p.projections.known;
    default: return p.leagues;
  }
}

/** The heaviest bye week ahead (most of your active rostered players on bye), or null when none has a bye. */
export function heaviestByeWeek(weeks: readonly PortfolioWeek[]): PortfolioWeek | null {
  let best: PortfolioWeek | null = null;
  for (const w of weeks) {
    if (w.bye.exposures > 0 && (!best || w.bye.exposures > best.bye.exposures)) best = w;
  }
  return best;
}

/** `?query` for this page with some filters changed (null removes one), keeping the sort and every other param. */
export function filterHref(sp: SearchParams | undefined, changes: Record<string, string | null>): string {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(sp ?? {})) {
    if (k in changes || v === undefined) continue;
    for (const item of Array.isArray(v) ? v : [v]) qs.append(k, item);
  }
  for (const [k, v] of Object.entries(changes)) if (v !== null && v !== "") qs.set(k, v);
  const s = qs.toString();
  return s ? `?${s}` : "?";
}
