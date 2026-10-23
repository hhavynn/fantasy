// Trade scenario watchlist (issue #183): up to MAX_SCENARIOS trades a manager is weighing, kept on this device under
// the canonical league ID and the team it is for, so they can be revisited and compared without reselecting players.
// A scenario is a sanitized record: both team IDs, the player IDs each team sends, the start week, when it was saved
// and the league data version (signal-provenance.v1 snapshot_version) it was saved on. No names, deltas or provider
// data are stored, so an old result can never be shown as current. The backend compares the list
// (GET /api/trades/scenarios, contract trade-scenario-compare/v1, through the /api/trade-scenarios route) against the
// league's current files. Nothing here proposes or submits a trade, or talks to ESPN or Sleeper.

import { isLeagueId, leaguePath } from "./league-scope.ts";
import type { ProvenancePayload } from "./signal-provenance.ts";
import { analyzerHref } from "./trade-discovery.ts";

/** The backend compares at most this many (app/analytics/trade_scenarios.py MAX_SCENARIOS). */
export const MAX_SCENARIOS = 5;
/** Request bound per side (the analyzer's own per-side limit is checked by the backend, per scenario). */
export const MAX_IDS_PER_SIDE = 12;

export type Scenario = {
  id: string;
  ta: number; tb: number;
  /** Team A sends / Team B sends (player IDs). */
  a: number[]; b: number[];
  /** Explicit first week (null: the earliest actionable week). */
  sw: number | null;
  savedAt: string;
  /** The league data version the scenario was saved on; null when unknown. Compared for equality only. */
  dataVersion: string | null;
};
export type ScenarioSelection = Pick<Scenario, "ta" | "tb" | "a" | "b" | "sw">;
type Stored = { v: 1; league: string; team: number; scenarios: Scenario[] };

const validId = (id: unknown): id is number => typeof id === "number" && Number.isSafeInteger(id) && id !== 0 && Math.abs(id) <= 1e12;
const validTeam = (t: unknown): t is number => typeof t === "number" && Number.isSafeInteger(t) && t >= 0 && t <= 999_999;
const validWeek = (w: unknown): w is number => typeof w === "number" && Number.isSafeInteger(w) && w >= 1 && w <= 30;
const SCENARIO_ID = /^[A-Za-z0-9_-]{1,40}$/;
const SAVED_AT = /^\d{4}-\d{2}-\d{2}T[\d:.]{5,15}Z$/;
const VERSION = /^[\x21-\x7e]{1,200}$/;

/** No shared fallback: the watchlist needs a canonical league ID and a known team, or it is not stored at all. */
export function tradeScenariosKey(leagueId: string | null, team: number | null): string | null {
  return isLeagueId(leagueId) && validTeam(team) ? `fi:trade-scenarios:v1:${leagueId}:${team}` : null;
}

/** The scenario this browser tab last opened in the analyzer (to offer "Update"), per league and team. */
export function editingKey(leagueId: string | null, team: number | null): string | null {
  return isLeagueId(leagueId) && validTeam(team) ? `fi:trade-scenario-editing:v1:${leagueId}:${team}` : null;
}

function validSide(ids: unknown): ids is number[] {
  return Array.isArray(ids) && ids.length >= 1 && ids.length <= MAX_IDS_PER_SIDE && ids.every(validId);
}

/** A well-formed selection: two different teams, 1 to MAX_IDS_PER_SIDE players a side, no player twice. */
export function validSelection(s: Partial<ScenarioSelection> | null | undefined): s is ScenarioSelection {
  if (!s || !validTeam(s.ta) || !validTeam(s.tb) || s.ta === s.tb || !validSide(s.a) || !validSide(s.b)) return false;
  if (s.sw !== null && !validWeek(s.sw)) return false;
  return new Set([...s.a, ...s.b]).size === s.a.length + s.b.length;
}

/**
 * The stored scenarios, or none. The payload repeats its league and team, so a value copied under another key (another
 * league, another team) is refused rather than carried over. Any malformed scenario refuses the whole value.
 */
export function parseTradeScenarios(raw: string | null, leagueId: string, team: number): Scenario[] {
  try {
    const v = JSON.parse(raw ?? "null") as Partial<Stored> | null;
    if (!v || v.v !== 1 || v.league !== leagueId || v.team !== team || !Array.isArray(v.scenarios)) return [];
    if (v.scenarios.length > MAX_SCENARIOS) return [];
    const ids = new Set<string>();
    const out: Scenario[] = [];
    for (const x of v.scenarios as unknown[]) {
      if (!x || typeof x !== "object") return [];
      const { id, ta, tb, a, b, sw, savedAt, dataVersion } = x as Record<string, unknown>;
      const sel = { ta, tb, a, b, sw } as Partial<ScenarioSelection>;
      if (typeof id !== "string" || !SCENARIO_ID.test(id) || ids.has(id) || !validSelection(sel)) return [];
      if (typeof savedAt !== "string" || !SAVED_AT.test(savedAt)) return [];
      if (dataVersion !== null && (typeof dataVersion !== "string" || !VERSION.test(dataVersion))) return [];
      ids.add(id);
      out.push({ id, ta: sel.ta, tb: sel.tb, a: [...sel.a], b: [...sel.b], sw: sel.sw, savedAt, dataVersion: dataVersion as string | null });
    }
    return out;
  } catch {
    return [];
  }
}

export function serializeTradeScenarios(leagueId: string, team: number, scenarios: Scenario[]): string {
  const clean = scenarios.map(({ id, ta, tb, a, b, sw, savedAt, dataVersion }) => ({ id, ta, tb, a, b, sw, savedAt, dataVersion }));
  return JSON.stringify({ v: 1, league: leagueId, team, scenarios: clean } satisfies Stored);
}

export function readTradeScenarios(storage: Pick<Storage, "getItem">, leagueId: string | null, team: number | null): { scenarios: Scenario[]; available: boolean } {
  const key = tradeScenariosKey(leagueId, team);
  if (!key) return { scenarios: [], available: false };
  try {
    return { scenarios: parseTradeScenarios(storage.getItem(key), leagueId!, team!), available: true };
  } catch {
    return { scenarios: [], available: false };
  }
}

export function writeTradeScenarios(storage: Pick<Storage, "setItem">, leagueId: string | null, team: number | null, scenarios: Scenario[]): boolean {
  const key = tradeScenariosKey(leagueId, team);
  if (!key) return false;
  try {
    storage.setItem(key, serializeTradeScenarios(leagueId!, team!, scenarios.slice(0, MAX_SCENARIOS)));
    return true;
  } catch {
    return false;
  }
}

// ---- editing the list ---------------------------------------------------------------------------------------------

/** The same trade whichever side is called A (the start week is the comparison's, not the trade's). */
export function tradeKey(s: Pick<ScenarioSelection, "ta" | "tb" | "a" | "b">): string {
  const side = (t: number, ids: number[]) => `${t}:${[...ids].sort((p, q) => p - q).join(".")}`;
  return [side(s.ta, s.a), side(s.tb, s.b)].sort().join("|");
}

export function findTrade(list: readonly Scenario[], s: Pick<ScenarioSelection, "ta" | "tb" | "a" | "b">): Scenario | null {
  const k = tradeKey(s);
  return list.find((x) => tradeKey(x) === k) ?? null;
}

export type Meta = { id: string; savedAt: string; dataVersion: string | null };
export type Edit = { scenarios: Scenario[]; outcome: "added" | "updated" | "duplicate" | "full" | "invalid" | "missing"; id: string | null };

/** Save a new scenario at the end. An identical trade already saved, a full list or a malformed selection is refused. */
export function addScenario(list: readonly Scenario[], sel: ScenarioSelection, meta: Meta): Edit {
  if (!validSelection(sel) || !SCENARIO_ID.test(meta.id)) return { scenarios: [...list], outcome: "invalid", id: null };
  const same = findTrade(list, sel);
  if (same) return { scenarios: [...list], outcome: "duplicate", id: same.id };
  if (list.length >= MAX_SCENARIOS) return { scenarios: [...list], outcome: "full", id: null };
  return { scenarios: [...list, { ...pick(sel), ...meta }], outcome: "added", id: meta.id };
}

/** Replace scenario `id` with `sel` (saved now, on the current data version), keeping its place and ID. */
export function replaceScenario(list: readonly Scenario[], id: string, sel: ScenarioSelection, meta: Omit<Meta, "id">): Edit {
  const i = list.findIndex((x) => x.id === id);
  if (i < 0) return { scenarios: [...list], outcome: "missing", id: null };
  if (!validSelection(sel)) return { scenarios: [...list], outcome: "invalid", id: null };
  const same = findTrade(list, sel);
  if (same && same.id !== id) return { scenarios: [...list], outcome: "duplicate", id: same.id };
  const out = [...list];
  out[i] = { ...pick(sel), id, ...meta };
  return { scenarios: out, outcome: "updated", id };
}

/** A copy of scenario `id` right after it (a starting point for a variant: open it, change it, update it). */
export function duplicateScenario(list: readonly Scenario[], id: string, meta: Pick<Meta, "id" | "savedAt">): Edit {
  const i = list.findIndex((x) => x.id === id);
  if (i < 0) return { scenarios: [...list], outcome: "missing", id: null };
  if (list.length >= MAX_SCENARIOS) return { scenarios: [...list], outcome: "full", id: null };
  if (!SCENARIO_ID.test(meta.id) || list.some((x) => x.id === meta.id)) return { scenarios: [...list], outcome: "invalid", id: null };
  const out = [...list];
  // The copy keeps the original's data version: it was built on the same data.
  out.splice(i + 1, 0, { ...list[i], a: [...list[i].a], b: [...list[i].b], id: meta.id, savedAt: meta.savedAt });
  return { scenarios: out, outcome: "added", id: meta.id };
}

export type SaveState =
  /** This exact trade and start week is saved. */
  | { kind: "saved"; index: number }
  /** The same trade is saved with another start week: Update changes the stored start week (one entry per trade). */
  | { kind: "start_week"; index: number; id: string; savedWeek: number | null }
  /** Not saved: Save adds it; `editing` is the opened scenario Update would replace (null: none). */
  | { kind: "new"; editing: { index: number; id: string } | null };

/**
 * What the save bar offers for the evaluated selection. A trade is stored once whatever its start week (the comparison
 * uses one shared horizon), but the stored start week is part of the scenario, so a changed start week is an update.
 */
export function saveState(list: readonly Scenario[], sel: ScenarioSelection, editingId: string | null): SaveState {
  const same = findTrade(list, sel);
  if (same) {
    const index = list.indexOf(same);
    return same.sw === sel.sw ? { kind: "saved", index } : { kind: "start_week", index, id: same.id, savedWeek: same.sw };
  }
  const index = editingId === null ? -1 : list.findIndex((s) => s.id === editingId);
  return { kind: "new", editing: index >= 0 ? { index, id: list[index].id } : null };
}

export function removeScenario(list: readonly Scenario[], id: string): Scenario[] {
  return list.filter((x) => x.id !== id);
}

function pick(s: ScenarioSelection): ScenarioSelection {
  return { ta: s.ta, tb: s.tb, a: [...s.a], b: [...s.b], sw: s.sw };
}

// ---- checks against the current page data --------------------------------------------------------------------------

export type RosterTeam = { team_id: number; name: string; roster: { espn_player_id: number; name: string | null }[] };
export type Moved = { id: number; from: number; now: number | null };

/** Players no longer on the roster that sends them in the current page data (the backend checks again on compare). */
export function rosterMoves(s: Pick<Scenario, "ta" | "tb" | "a" | "b">, teams: readonly RosterTeam[]): Moved[] {
  const where = new Map<number, number>();
  for (const t of teams) for (const p of t.roster) where.set(p.espn_player_id, t.team_id);
  const out: Moved[] = [];
  for (const [team, ids] of [[s.ta, s.a], [s.tb, s.b]] as const) {
    for (const id of ids) {
      const now = where.get(id) ?? null;
      if (now !== team) out.push({ id, from: team, now });
    }
  }
  return out;
}

/** "different": another opaque data version (compared for equality only, so neither older nor newer is implied). */
export type SavedFreshness = "current" | "different" | "unknown";

/** Whether a scenario was saved on the data version the page now reads. Unknown on either side is never "current". */
export function savedFreshness(saved: string | null, current: string | null | undefined): SavedFreshness {
  if (saved === null || !current) return "unknown";
  return saved === current ? "current" : "different";
}

// ---- requests -------------------------------------------------------------------------------------------------------

/** `TA:TB:A1.A2:B1.B2` per scenario, as the backend reads it. */
export function scenarioParam(s: Pick<ScenarioSelection, "ta" | "tb" | "a" | "b">): string {
  return `${s.ta}:${s.tb}:${s.a.join(".")}:${s.b.join(".")}`;
}

/**
 * The browser URL that compares `scenarios` in `leagueId` over one horizon: always under /l/<lg>/, so the proxy scopes
 * the request to that league; there is no unscoped form. The list is one ordered parameter (its order is the saved
 * order, and one value keys the league-scoped response cache on exactly this list).
 */
export function scenariosSrc(leagueId: string, scenarios: readonly Pick<ScenarioSelection, "ta" | "tb" | "a" | "b">[], startWeek: number | null): string {
  const q = new URLSearchParams({ scenarios: scenarios.slice(0, MAX_SCENARIOS).map(scenarioParam).join(",") });
  if (startWeek !== null) q.set("start_week", String(startWeek));
  return `${leaguePath(leagueId, "/api/trade-scenarios")}?${q}`;
}

const ID = String.raw`-?\d{1,13}`;
const SCENARIO = new RegExp(String.raw`^\d{1,6}:\d{1,6}:${ID}(\.${ID}){0,${MAX_IDS_PER_SIDE - 1}}:${ID}(\.${ID}){0,${MAX_IDS_PER_SIDE - 1}}$`);

/** The backend path for the browser's query: only a well-formed `scenarios` list and `start_week` are forwarded. */
export function scenariosBackendPath(params: URLSearchParams): string | null {
  const raw = params.get("scenarios") ?? "";
  const list = raw === "" ? [] : raw.split(",");
  if (list.length < 1 || list.length > MAX_SCENARIOS || list.some((s) => !SCENARIO.test(s))) return null;
  const q = new URLSearchParams({ scenarios: list.join(",") });
  const sw = params.get("start_week");
  if (sw !== null) {
    if (!/^\d{1,2}$/.test(sw) || !validWeek(Number(sw))) return null;
    q.set("start_week", sw);
  }
  return `/api/trades/scenarios?${q}`;
}

/** The browser-facing message when the backend comparison fails. */
export const COMPARE_UNAVAILABLE = "The comparison service is unavailable.";

/**
 * The /api/trade-scenarios route's error answer. The backend's own message is forwarded only for the answers it writes
 * for the manager (404 league or team not found, 409 no remaining weeks, 422 a bad horizon or list). Anything else
 * (offline, an unreadable answer, a server error) gets a generic message: the internal API address or a start hint
 * never reaches the browser. The status is the backend's only for an HTTP error; anything else (offline, or a 2xx
 * whose body is not JSON, which api() reports with that 2xx status) answers 502, never a success status.
 */
export function compareRouteError(res: { error: string; kind?: string; status?: number }): { body: { error: string; kind: string }; status: number } {
  const status = res.kind === "http" && res.status !== undefined && res.status >= 400 ? res.status : 502;
  const own = res.kind === "http" && (status === 404 || status === 409 || status === 422);
  return { body: { error: own ? res.error : COMPARE_UNAVAILABLE, kind: own ? "http" : (res.kind ?? "unknown") }, status };
}

/** The first evaluated week's column header: "Next week" only when the comparison starts at the earliest open week. */
export function shortTermLabel(ev: Pick<ScenarioCompareResponse["evaluation"], "short_term_week" | "start_week_overridden">): string {
  if (ev.short_term_week === null) return "First week";
  return ev.start_week_overridden ? `Week ${ev.short_term_week}` : "Next week";
}

/** The summary line's counts, in words; zero counts are left out and the two failures are told apart. */
export function summaryText(s: ScenarioCompareResponse["summary"]): string {
  const parts = [`${s.evaluated} of ${s.scenarios} evaluated`];
  if (s.incomplete > 0) parts.push(`${s.incomplete} incomplete`);
  if (s.stale > 0) parts.push(`${s.stale} out of date`);
  if (s.invalid > 0) parts.push(`${s.invalid} not valid`);
  if ((s.unavailable ?? 0) > 0) parts.push(`${s.unavailable} not evaluated`);
  return parts.join(" · ");
}

/** The analyzer URL that reopens one scenario (both teams, both packages, its start week). Unscoped: callers add the league. */
export function scenarioAnalyzerHref(s: ScenarioSelection): string {
  const href = analyzerHref({ anchor_team_id: s.ta, opponent_team_id: s.tb, sends: s.a, receives: s.b });
  return s.sw === null ? href : `${href}&sw=${s.sw}`;
}

/** Start weeks saved with the scenarios that differ from the comparison's (all rows share one horizon). */
export function horizonMismatches(scenarios: readonly Scenario[], startWeek: number | null): number[] {
  return scenarios.flatMap((s, i) => (s.sw !== startWeek ? [i] : []));
}

// ---- the backend contract -----------------------------------------------------------------------------------------

export type ScenarioSide = {
  team_id: number; team_name: string | null; sends: number[]; receives: number[];
  short_term: { week: number; delta: number | null; known_delta: number; projection_complete: boolean } | null;
  regular_season_delta: number | null; playoff_delta: number | null; ros_delta: number | null; known_ros_delta: number;
  weeks_incomplete: number[]; projection_complete: boolean; missing_players: number[];
  forced_drops: { espn_player_id: number; name: string | null; position: string | null; is_incoming: boolean }[];
};
export type ScenarioRow = {
  index: number;
  request: { team_a_id: number; team_b_id: number; team_a_sends: number[]; team_b_sends: number[] };
  shape: string;
  status: "evaluated" | "stale" | "invalid" | "unavailable";
  reason: string | null;
  duplicate_of: number | null;
  conflicts_with: number[];
  moved: { espn_player_id: number; expected_team_id: number; current_team_id: number | null; current_team_name: string | null }[];
  team_a?: ScenarioSide; team_b?: ScenarioSide;
  result_label?: string;
};
export type ScenarioCompareResponse = {
  contract: "trade-scenario-compare/v1";
  submitted: false;
  max_scenarios: number;
  evaluation: { start_week: number; final_week: number; last_regular_season_week: number | null; weeks: number[];
    short_term_week: number | null; start_week_overridden: boolean; earliest_actionable_week: number | null };
  scenarios: ScenarioRow[];
  conflicts: { espn_player_id: number; scenarios: number[] }[];
  summary: { scenarios: number; evaluated: number; incomplete: number; stale: number; invalid: number; unavailable: number };
  projection_source: string | null;
  notes: string[];
  assumptions: string[];
  names: Record<string, { name: string | null; position: string | null; pro_team: string | null }>;
  teams: { team_id: number; name: string | null }[];
  data_version: string | null;
  provenance?: ProvenancePayload | null;
  /** Added by the /api/trade-scenarios route: the canonical league the answer was read from. */
  scope?: string;
  /** Added by the route: the backend served this league's previous files (X-FI-Degraded), so the data may be older. */
  stale?: boolean;
};

/** A response for this league that names this exact list, in order; anything else is not shown. */
export function matchesRequest(res: ScenarioCompareResponse | null, leagueId: string, scenarios: readonly ScenarioSelection[]): boolean {
  if (!res || res.contract !== "trade-scenario-compare/v1" || res.scope !== leagueId || res.scenarios.length !== scenarios.length) return false;
  return res.scenarios.every((row, i) => scenarioParam({ ta: row.request.team_a_id, tb: row.request.team_b_id, a: row.request.team_a_sends,
    b: row.request.team_b_sends }) === scenarioParam(scenarios[i]));
}

/** The sides of an evaluated row, the given team's first when it is in the trade (else as saved). */
export function orderedSides(row: ScenarioRow, team: number | null): ScenarioSide[] {
  if (!row.team_a || !row.team_b) return [];
  return team !== null && row.team_b.team_id === team ? [row.team_b, row.team_a] : [row.team_a, row.team_b];
}

/** Which rows a player-sharing conflict joins, as 1-based scenario numbers ("Scenarios 1 and 3"). */
export function conflictText(c: { scenarios: number[] }, name: string): string {
  const n = c.scenarios.map((i) => i + 1);
  const list = n.length <= 2 ? n.join(" and ") : `${n.slice(0, -1).join(", ")} and ${n[n.length - 1]}`;
  return `Scenarios ${list} ${n.length === 2 ? "both" : "all"} include ${name}: at most one of them can happen as saved.`;
}
