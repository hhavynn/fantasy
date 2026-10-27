// League-wide team perspective: which fantasy team a league-personalized page analyzes.
// The selection lives in the URL (`?team=<id>`) so a view can be shared with leaguemates and the server-rendered
// page (and every backend call it makes) sees it. No team in the URL = the owner's team, as before.

export const TEAM_PARAM = "team";
/** Older links (Start/Sit, Matchup) used `team_id`; it is still read, never written. */
const LEGACY_TEAM_PARAM = "team_id";

/** Pages whose numbers depend on the selected team. Navigating between them keeps the selection. */
export const TEAM_SCOPED_PATHS = [
  "/", "/matchup", "/start-sit", "/my-team/value", "/waivers", "/trades", "/trades/discover",
  "/schedule-intelligence", "/schedule-intelligence/sos", "/luck-lab", "/playoff-race", "/power-rankings", "/injury-impact",
  "/league-analyzer",
  "/injury-impact/timeline", "/ask",
] as const;

/** Params that only make sense for one team (an opponent, a counterfactual schedule, a page offset, a Start/Sit
 *  what-if lineup, a Waivers add/drop scenario, a Compare shortlist's start pick) and are dropped when the team changes. */
export const TEAM_SPECIFIC_PARAMS = ["opponent", "vs", "schedule_of", "offset", "player", "ta", "tb", "a", "b", "override", "whatif", "add", "drop", "start"];

type SP = Record<string, string | string[] | undefined>;
const first = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);

export type TeamSelection =
  | { kind: "default" }                       // no team in the URL: the owner's team
  | { kind: "team"; id: number }
  | { kind: "invalid"; raw: string };         // not a team id at all (the backend never sees it)

/** The team asked for in the URL: `team`, else the legacy `team_id`. */
export function readTeamSelection(sp: SP): TeamSelection {
  const raw = first(sp[TEAM_PARAM]) ?? first(sp[LEGACY_TEAM_PARAM]);
  if (raw === undefined || raw.trim() === "") return { kind: "default" };
  return /^\d{1,6}$/.test(raw.trim()) ? { kind: "team", id: Number(raw.trim()) } : { kind: "invalid", raw };
}

/** The team id to send to the backend, or null for the owner default. */
export function selectedTeamId(sel: TeamSelection): number | null {
  return sel.kind === "team" ? sel.id : null;
}

/** Add `team_id` to a backend path when a team is selected; the default leaves the path unchanged. */
export function withTeamId(path: string, sel: TeamSelection | number | null): string {
  const id = typeof sel === "object" && sel !== null ? selectedTeamId(sel) : sel;
  if (id === null) return path;
  const [base, query = ""] = path.split("?");
  const qs = new URLSearchParams(query);
  qs.set("team_id", String(id));
  return `${base}?${qs}`;
}

function normalizePath(pathname: string): string {
  return pathname.length > 1 && pathname.endsWith("/") ? pathname.slice(0, -1) : pathname;
}

export function isTeamScoped(pathname: string): boolean {
  return (TEAM_SCOPED_PATHS as readonly string[]).includes(normalizePath(pathname));
}

/**
 * Whether the page at `pathname` with this query depends on the selected team: a team-scoped path, or the Compare
 * shortlist (`/compare?ids=…` without a pair), whose roster status is computed for `team`. The pair view and every
 * other `/compare` URL stay league-wide. The rail picker, link propagation and the page's own picker all use this, so
 * the team in the rail, the URL and the backend call agree on the shortlist as on any team-scoped page.
 */
export function isTeamScopedView(pathname: string, search?: URLSearchParams | SP | null): boolean {
  if (isTeamScoped(pathname)) return true;
  if (normalizePath(pathname) !== "/compare" || !search) return false;
  const has = (k: string) => (search instanceof URLSearchParams ? search.has(k) : search[k] !== undefined);
  return has("ids") && !has("a") && !has("b");
}

/**
 * `href` carrying the current team selection, when it points at a team-scoped page. An href that already names a
 * team keeps it; other pages, external links and the owner default are left as they are.
 */
export function withTeam(href: string, team: string | null | undefined): string {
  if (!team || !href.startsWith("/")) return href;
  const [pathAndQuery, hash] = href.split("#");
  const [path, query = ""] = pathAndQuery.split("?");
  const qs = new URLSearchParams(query);
  if (!isTeamScopedView(path, qs)) return href;
  if (qs.has(TEAM_PARAM) || qs.has(LEGACY_TEAM_PARAM)) return href;
  qs.set(TEAM_PARAM, team);
  return `${path}?${qs}${hash !== undefined ? `#${hash}` : ""}`;
}

/** The team param of the current URL as a string, for link propagation (null when absent or not an id). */
export function teamParamFrom(search: URLSearchParams | SP | null | undefined): string | null {
  if (!search) return null;
  const sel = search instanceof URLSearchParams
    ? readTeamSelection(Object.fromEntries([TEAM_PARAM, LEGACY_TEAM_PARAM].flatMap((k) => {
      const v = search.get(k);
      return v === null ? [] : [[k, v]];
    })))
    : readTeamSelection(search);
  return sel.kind === "team" ? String(sel.id) : null;
}

/**
 * The URL to load when the viewer picks another team on `pathname`: every current param is kept except the
 * team itself and the ones that only make sense for the previous team. `team === null` returns to the default.
 */
export function perspectiveHref(pathname: string, sp: SP | URLSearchParams, team: number | null,
                                reset: readonly string[] = TEAM_SPECIFIC_PARAMS): string {
  const entries: [string, string][] = sp instanceof URLSearchParams
    ? [...sp.entries()]
    : Object.entries(sp).flatMap(([k, v]) => (v === undefined ? [] : (Array.isArray(v) ? v : [v]).map((x) => [k, x] as [string, string])));
  const drop = new Set([TEAM_PARAM, LEGACY_TEAM_PARAM, ...reset]);
  const qs = new URLSearchParams(entries.filter(([k]) => !drop.has(k)));
  if (team !== null) qs.set(TEAM_PARAM, String(team));
  const s = qs.toString();
  return s ? `${pathname}?${s}` : pathname;
}

export type PerspectiveTeam = {
  team_id: number; name: string; abbrev: string | null; is_owner_default: boolean;
  roster_synced_at: string | null; roster_players: number;
};

export type TeamsPayload = {
  league: { league_id: number; season: number; name: string; current_week: number; synced_at: string | null };
  teams: PerspectiveTeam[];
  default_team_id: number | null;
};

/** The `perspective` block the overview and waivers endpoints return. */
export type Perspective = PerspectiveTeam & {
  source: "query" | "owner_default"; league_id: number; season: number; league_synced_at: string | null;
};

/** A team's name for a picker: the owner's team is the one shown when nobody picks, so it is marked "default". */
export function teamOptionLabel(t: { name: string; is_owner_default?: boolean; is_mine?: boolean }): string {
  return (t.is_owner_default ?? t.is_mine) ? `${t.name} (default)` : t.name;
}

/** Whether an API failure is the backend refusing the requested team (unknown, stale or deleted id). */
/** Only a missing default owner team deserves the picker; never replace an unauthorized league or arbitrary 404. */
export function needsDefaultTeam(res: {ok:boolean; status?:number|null; error?:string|null}): boolean {
  return !res.ok && (res.status === 404 || res.status === 422) && /No team is marked as yours|Your team was not identified/i.test(res.error ?? "");
}

export function isUnknownTeam(res: { ok: boolean; status?: number; error?: string | null }, sel: TeamSelection): boolean {
  return !res.ok && sel.kind !== "default" && res.status === 404 && /team/i.test(res.error ?? "");
}

/**
 * The team that links should carry from the page at `pathname`. On a team-scoped page the URL is the truth (no
 * `team` = the default, an explicit choice). League-wide pages never carry `team` in their own URL, so they fall
 * back to the team last analyzed in this browser tab: Start/Sit for team 4 → Rankings → My team opens team 4's
 * Overview again instead of silently switching back to the default.
 */
export function navTeam(pathname: string, urlTeam: string | null, lastTeam: string | null,
                        scoped: boolean = isTeamScoped(pathname)): string | null {
  return scoped ? urlTeam : (urlTeam ?? lastTeam);
}

/** A stored last-analyzed team, or null when absent or not a team id. */
export function parseStoredTeam(raw: string | null): string | null {
  return raw !== null && /^\d{1,6}$/.test(raw) ? raw : null;
}
