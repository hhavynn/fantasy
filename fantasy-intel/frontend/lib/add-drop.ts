// Add/drop scenario simulator on Waivers (issue #178). The backend (GET /api/waivers/add-drop, contract
// waiver-add-drop/v1) owns every rule: who is available, roster capacity, IR and taxi, the lineup simulation and the
// totals. This module only validates the URL, builds links and reads the answer. The scenario lives in the `add` and
// `drop` query parameters, so it can be shared and Back undoes it. Nothing here talks to ESPN or Sleeper.

import type { RosterLegality } from "./lineup-coverage.ts";
import type { ProvenancePayload } from "./signal-provenance.ts";

export const ADD_PARAM = "add";
export const DROP_PARAM = "drop";
export const ADD_DROP_PARAMS = [ADD_PARAM, DROP_PARAM] as const;
/** A provider player id: ESPN ids are positive, team defenses negative (-16002). The backend bounds them too. */
const PLAYER_ID = /^-?\d{1,12}$/;

type SP = Record<string, string | string[] | undefined>;
const first = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);

export type AddDropSelection =
  | { kind: "none" }
  | { kind: "pair"; add: number | null; drop: number | null }
  /** A value that is clearly not a player id, or the same player twice: never sent to the backend. */
  | { kind: "invalid" };

/** The scenario in the URL. Each side is optional; a malformed side makes the whole scenario invalid. */
export function readAddDrop(sp: SP): AddDropSelection {
  const raw = { add: first(sp[ADD_PARAM]), drop: first(sp[DROP_PARAM]) };
  const parsed: { add: number | null; drop: number | null } = { add: null, drop: null };
  for (const key of ["add", "drop"] as const) {
    const v = raw[key]?.trim();
    if (v === undefined || v === "") continue;
    if (!PLAYER_ID.test(v)) return { kind: "invalid" };
    parsed[key] = Number(v);
  }
  if (parsed.add === null && parsed.drop === null) return { kind: "none" };
  if (parsed.add !== null && parsed.add === parsed.drop) return { kind: "invalid" };
  return { kind: "pair", ...parsed };
}

/** The page URL with the scenario set (a null side removed); every other parameter is kept, `hash` appended. */
export function addDropHref(pathname: string, sp: SP | URLSearchParams, pair: { add: number | null; drop: number | null },
  hash = "add-drop"): string {
  const entries: [string, string][] = sp instanceof URLSearchParams
    ? [...sp.entries()]
    : Object.entries(sp).flatMap(([k, v]) => (v === undefined ? [] : (Array.isArray(v) ? v : [v]).map((x) => [k, x] as [string, string])));
  const qs = new URLSearchParams(entries.filter(([k]) => !(ADD_DROP_PARAMS as readonly string[]).includes(k)));
  if (pair.add !== null) qs.set(ADD_PARAM, String(pair.add));
  if (pair.drop !== null) qs.set(DROP_PARAM, String(pair.drop));
  const s = qs.toString();
  // A relative href ("" path) keeps the page's own path, league scope included; "?" alone clears the query.
  return `${pathname}${s ? `?${s}` : pathname ? "" : "?"}${hash ? `#${hash}` : ""}`;
}

/** The backend path with the scenario added. */
export function withAddDrop(apiPath: string, sel: AddDropSelection): string {
  if (sel.kind !== "pair") return apiPath;
  const [base, query = ""] = apiPath.split("?");
  const qs = new URLSearchParams(query);
  if (sel.add !== null) qs.set(ADD_PARAM, String(sel.add));
  if (sel.drop !== null) qs.set(DROP_PARAM, String(sel.drop));
  return `${base}?${qs}`;
}

/** The other query parameters as hidden form fields, so the no-JavaScript form keeps the team, view and filters. */
export function carriedParams(sp: SP): [string, string][] {
  return Object.entries(sp)
    .filter(([k]) => !(ADD_DROP_PARAMS as readonly string[]).includes(k) && k !== "offset")
    .flatMap(([k, v]) => (v === undefined ? [] : (Array.isArray(v) ? v : [v]).map((x) => [k, x] as [string, string])));
}

// ---- the backend contract -----------------------------------------------------------------------------------------

export type AddDropItem = { code: string; player_id: number | null; message: string; tone: "info" | "caution" };

export type AddDropOption = {
  espn_player_id: number; name: string | null; position: string | null; pro_team: string | null;
  injury_status: string | null; known_projected_points: number | null; projected_weeks: number;
  availability_status?: string | null; on_ir?: boolean; on_taxi?: boolean;
};

export type AddDropPlayer = {
  espn_player_id: number; name: string | null; position: string | null; pro_team: string | null;
  injury_status?: string | null;
  roster_status: "free_agent" | "waivers" | "own_roster" | "other_roster" | "not_available" | "not_rostered";
  availability_status?: string | null; team_id?: number | null; team_name?: string | null;
  on_ir?: boolean; on_taxi?: boolean; lineup_eligible?: boolean;
  weekly_projection?: Record<string, number | null>;
  nfl_ineligible_weeks?: number[];
  /** Remaining weeks his NFL team has no game; null when the schedule does not cover the window. */
  bye_weeks?: number[] | null;
  kickoff_lock?: { week: number | null; status: string; reason: string; kickoff_utc: string | null } | null;
};

export type AddDropWeek = {
  week: number; phase: "regular" | "playoff" | null;
  before: number | null; after: number | null; known_before: number; known_after: number;
  delta: number | null; known_delta: number; projection_complete: boolean;
  entered: { espn_player_id: number; name: string | null }[];
  exited: { espn_player_id: number; name: string | null }[];
  open_slots_before: number; open_slots_after: number;
  on_bye_before: number[] | null; on_bye_after: number[] | null;
  missing_projection_ids: number[];
};

export type AddDropRos = {
  ros_delta: number | null; known_ros_delta: number;
  regular_season_delta: number | null; playoff_delta: number | null; average_weekly_delta: number | null;
  weeks_improved: number; weeks_worsened: number; weeks_unchanged: number; weeks_incomplete: number[];
  projection_complete: boolean;
  /** Starting slots no engine fills (issue #253 G3): totals are partial; a delta is exact only when it can be. */
  unsupported_slots?: string[];
};

export type AddDropDepth = {
  position: string; starting_slots: number; flex_slots: { slot: string; count: number }[];
  before: { active: number; reserved: number; players: { espn_player_id: number; name: string | null; reserved: boolean }[] };
  after: { active: number; reserved: number; players: { espn_player_id: number; name: string | null; reserved: boolean }[] } | null;
  active_change: number | null;
};

export type AddDropResponse = {
  contract: "waiver-add-drop/v1";
  status: "options" | "evaluated" | "blocked";
  team: { id: number | null; name: string | null };
  request: { add: number | null; drop: number | null };
  evaluation: {
    start_week: number | null; final_week: number | null; last_regular_season_week: number | null; weeks: number[];
    earliest_actionable_week: number | null; uncertain: boolean; warning: string | null;
    start_week_reason: { reason?: string; rule?: string } & Record<string, unknown>; evaluated_at: string | null;
  };
  projection_source: string;
  problems: AddDropItem[];
  notes: AddDropItem[];
  players: { add: AddDropPlayer | null; drop: AddDropPlayer | null };
  options: { add: AddDropOption[]; add_total: number; add_limit: number; drop: AddDropOption[] };
  roster: {
    active_capacity: number; position_limits: Record<string, number>;
    active_before: number | null; active_after: number | null; open_spots_before: number | null; open_spots_after: number | null;
    legal: boolean | null; drop_required: boolean | null; ir_slot_blockers: number[];
    /** Present when legality cannot be verified (slots no engine fills, players missing from the data): `legal` is then
     * never true and no open-spot count is given (issue #253 G4). */
    legality?: RosterLegality;
    suggested_drop: ({ available: true; espn_player_id: number; name: string | null; position: string | null;
      lineup_cost: number | null; known_lineup_cost: number; basis: string; rule: string }
      | { available: false; reason: string }) | null;
  };
  depth: AddDropDepth[];
  impact: { first_week: AddDropWeek | null; rest_of_season: AddDropRos; weeks: AddDropWeek[] } | null;
  acquisition: { kind: string; summary: string; claim_required: boolean; availability_status: string | null } | null;
  assumptions: string[];
  limitations: string[];
  /** signal-provenance.v1: league data, the rest-of-season projections and injury reports. */
  provenance?: ProvenancePayload | null;
};

// ---- display rules ------------------------------------------------------------------------------------------------

/** A change as shown: the full delta, or the known-only sum marked partial (a missing projection is unknown, not 0). */
export function shownChange(full: number | null, known: number | null | undefined): { value: number | null; partial: boolean } {
  if (full !== null) return { value: full, partial: false };
  return known === null || known === undefined ? { value: null, partial: false } : { value: known, partial: true };
}

/** "Weeks 3–17" or "Week 17". */
export function windowLabel(weeks: number[]): string | null {
  if (weeks.length === 0) return null;
  const a = weeks[0];
  const b = weeks[weeks.length - 1];
  return a === b ? `Week ${a}` : `Weeks ${a}–${b}`;
}

/** One picker option: "Waiver RB (RB, KC) · 180.0 proj · waivers". Never a rostered player for the add side. */
export function optionLabel(o: AddDropOption): string {
  const who = `${o.name ?? `Player ${o.espn_player_id}`} (${[o.position, o.pro_team ?? "FA"].filter(Boolean).join(", ")})`;
  const pts = o.known_projected_points === null ? "no projection" : `${o.known_projected_points.toFixed(1)} proj`;
  const tags = [o.availability_status === "WAIVERS" ? "waivers" : null, o.on_ir ? "IR" : null, o.on_taxi ? "taxi" : null]
    .filter(Boolean);
  return [who, pts, ...tags].join(" · ");
}

/** The answer in one sentence: the move and what it does to the first week and the rest of the season. */
export function headline(d: AddDropResponse): string {
  const add = d.players.add?.name ?? null;
  const drop = d.players.drop?.name ?? null;
  const move = add && drop ? `Add ${add}, drop ${drop}` : add ? `Add ${add}` : drop ? `Drop ${drop}` : "Choose a move";
  if (d.status === "options") return "Choose a player to add, one to drop, or both";
  if (d.status === "blocked") return `${move}: not possible as asked`;
  const ros = d.impact ? shownChange(d.impact.rest_of_season.ros_delta, d.impact.rest_of_season.known_ros_delta) : null;
  if (!ros || ros.value === null) return d.roster.legality ? `${move}: roster legality unverified` : move;
  const dir = Math.abs(ros.value) < 0.05 ? "no change to your projected lineups" : ros.value > 0 ? "a projected lineup gain" : "a projected lineup loss";
  return `${move}: ${dir}${ros.partial ? " (known projections only)" : ""}${d.roster.legality ? "; roster legality unverified" : ""}`;
}
