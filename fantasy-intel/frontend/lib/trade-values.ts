// Types and pure helpers for the Trade values board (GET /api/trades/values, contract "trade-values/v1", issue #184).
// Every value, rank, change and caveat comes from the backend; these helpers validate the URL, filter, order and
// phrase those numbers. They never compute a value, never turn a missing value or change into 0, and never call a
// value a price.
import type { ProvenancePayload } from "./signal-provenance.ts";
import type { SortState } from "./sort.ts";
import { weekSpan } from "./projection-movers.ts";

export const CONTRACT = "trade-values/v1";
export const PATH = "/trades/values";

export const POSITIONS = ["QB", "RB", "WR", "TE", "K", "D/ST"] as const;
export type Position = (typeof POSITIONS)[number];

// ----------------------------------------------------------------------------- API contract

export type WeekRow = {
  week: number;
  phase: "regular" | "playoff" | null;
  projection: number | null;
  replacement: number | null;
  replacement_source: "free_agent" | "league_structure" | null;
  adds: number | null;
  status: "counted" | "nfl_ineligible" | "missing";
};

export type Caveat = { code: string; text: string };

export type RowChange = {
  value_before: number | null;
  value_now: number | null;
  delta: number | null;
  reason: string | null;
  projection_change: number | null;
  replacement_change: number | null;
  position_rank_before: number | null;
  position_rank_now: number | null;
};

export type ValueRow = {
  espn_player_id: number;
  name: string | null;
  position: string | null;
  pro_team: string | null;
  injury_status: string | null;
  team_id: number;
  team_name: string | null;
  on_my_team: boolean;
  value: number | null;
  value_per_week: number | null;
  value_regular: number | null;
  value_playoff: number | null;
  known_value: number | null;
  rank: number | null;
  rank_of: number;
  position_rank: number | null;
  position_rank_of: number | null;
  league_starters_at_position: number | null;
  starter_caliber: boolean;
  weeks_counted: number;
  missing_weeks: number[];
  nfl_ineligible_weeks: number[];
  availability_status: string | null;
  uncertainty: Caveat[];
  weekly: WeekRow[];
  change: RowChange | null;
};

export type SnapshotRef = { id: string; label: string; taken_at: string | null; cycle_week: number | null; model_version: string | null };

export type ChangeContext = {
  available: boolean;
  reason: string | null;
  baseline: SnapshotRef | null;
  weeks: number[];
  excluded_weeks: { week: number; reason: string }[];
  notes: string[];
  method: string;
  source: "espn" | "independent";
};

export type Basis = {
  label: string;
  calculation_version: string;
  unit: string;
  calculation: string;
  comparable_within: string;
  projection_source: "espn" | "independent";
  projection_label: string;
  model_version: string | null;
  scoring: string;
  window: {
    weeks: number[]; start: number | null; final: number | null; last_regular_season_week: number | null;
    regular_weeks: number[]; playoff_weeks: number[]; uncertain: boolean; warning: string | null;
  };
  kdst_streaming: boolean;
  league_starters: Record<string, number>;
  flex_note: string | null;
  incomplete_team_weeks: number;
  replacement_fallback_positions: string[];
  rostered_only: string;
};

export type TradeValuesResponse = {
  contract: string;
  label: string;
  note: string;
  season: number;
  league: { name: string | null; current_week: number | null };
  my_team_id: number | null;
  teams: { team_id: number; name: string | null; is_mine: boolean }[];
  available: boolean;
  reason: string | null;
  basis: Basis | null;
  change: ChangeContext | null;
  players: ValueRow[];
  summary: { players: number; valued: number; unvalued: number; changed: number } | null;
  provenance: ProvenancePayload | null;
};

// ----------------------------------------------------------------------------- URL state

export type ValuesState = { position: Position | null; owner: number | null; player: number | null };

type SP = Record<string, string | string[] | undefined>;
const first = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);

function id(raw: string | undefined): number | null {
  if (raw === undefined || !/^-?\d{1,12}$/.test(raw.trim())) return null;
  return Number(raw.trim());
}

/** The filters in the URL. `owner` is a team id (not `team`, which is the app-wide perspective param). Unknown
 *  values are dropped, never guessed. */
export function parseValuesState(sp: SP): ValuesState {
  const pos = first(sp.pos);
  return {
    position: (POSITIONS as readonly string[]).includes(pos ?? "") ? (pos as Position) : null,
    owner: id(first(sp.owner)),
    player: id(first(sp.player)),
  };
}

/** The board's own URL with `updates`; any change but `player` clears the selected player. Sorting is kept. */
export function valuesHref(state: ValuesState, updates: Partial<ValuesState> = {}, sp: SP = {}): string {
  const next: ValuesState = { ...state, ...updates };
  if (!("player" in updates)) next.player = null;
  const qs = new URLSearchParams();
  if (next.position) qs.set("pos", next.position);
  if (next.owner !== null) qs.set("owner", String(next.owner));
  if (next.player !== null) qs.set("player", String(next.player));
  for (const k of ["sort", "dir"]) {
    const v = first(sp[k]);
    if (v) qs.set(k, v);
  }
  const q = qs.toString();
  return q ? `${PATH}?${q}` : PATH;
}

export function filterRows(rows: readonly ValueRow[], state: ValuesState): ValueRow[] {
  return rows.filter((r) => (state.position === null || r.position === state.position) && (state.owner === null || r.team_id === state.owner));
}

// ----------------------------------------------------------------------------- sorting

export const SORT_KEYS = ["player", "owner", "value", "per_week", "pos_rank", "change"] as const;
export const DEFAULT_SORT: SortState = { key: "value", dir: "desc" };

/** The sort value of a row; null (missing) always sorts last (lib/sort.ts), so an unknown value is never ranked as 0. */
export function valueSortValue(r: ValueRow, key: string): number | string | null {
  switch (key) {
    case "player": return r.name ?? `#${r.espn_player_id}`;
    case "owner": return r.team_name ?? `Team ${r.team_id}`;
    case "per_week": return r.value_per_week;
    case "pos_rank": return r.position_rank;
    case "change": return r.change?.delta ?? null;
    default: return r.value;
  }
}

// ----------------------------------------------------------------------------- links

/** The trade analyzer with this player on the right side: traded away from your team, or asked for from his owner.
 *  Without a team of yours the analyzer opens on his owner's side. */
export function analyzerHref(r: Pick<ValueRow, "espn_player_id" | "team_id">, myTeamId: number | null): string {
  if (myTeamId === null || r.team_id === myTeamId) return `/trades?ta=${r.team_id}&a=${r.espn_player_id}`;
  return `/trades?ta=${myTeamId}&tb=${r.team_id}&b=${r.espn_player_id}`;
}

export function analyzerLabel(r: Pick<ValueRow, "team_id">, myTeamId: number | null): string {
  return myTeamId !== null && r.team_id !== myTeamId ? "Trade for" : "Trade away";
}

export const ownerHref = (teamId: number): string => `/managers/${teamId}`;

// ----------------------------------------------------------------------------- wording

export const VALUE_MISSING = "No value: a remaining week has no projection, and a missing week is never counted as 0";

export function signedPts(v: number | null | undefined): string | null {
  if (v === null || v === undefined || Number.isNaN(v)) return null;
  const t = Math.abs(v).toFixed(1);
  return Number(t) === 0 ? "0.0" : `${v > 0 ? "+" : "−"}${t}`;
}

export function weeksText(weeks: readonly number[]): string {
  if (!weeks.length) return "no weeks";
  return `${weeks.length === 1 ? "week" : "weeks"} ${weekSpan(weeks)}`;
}

/** "RB3 of 12": rank among rostered players at the position with a known value. */
export function positionRankText(r: Pick<ValueRow, "position" | "position_rank" | "position_rank_of" | "league_starters_at_position">): string | null {
  if (r.position_rank === null || !r.position) return null;
  const of = r.position_rank_of !== null ? ` of ${r.position_rank_of}` : "";
  return `${r.position}${r.position_rank}${of}`;
}

function moved(v: number, up: string, down: string): string {
  const t = Math.abs(v).toFixed(1);
  return Number(t) === 0 ? "did not change" : `${v > 0 ? up : down} ${t}`;
}

/** Why a value moved, from the two measured parts. Null when there is no comparison at all. */
export function whyText(r: Pick<ValueRow, "position" | "change">, ctx: ChangeContext | null): string | null {
  const c = r.change;
  if (!ctx?.available || !c) return null;
  if (c.delta === null) return c.reason ?? "No change can be computed for this player.";
  const head = Number(Math.abs(c.delta).toFixed(1)) === 0
    ? "Value unchanged"
    : `Value ${c.delta > 0 ? "up" : "down"} ${Math.abs(c.delta).toFixed(1)}`;
  const since = ctx.baseline ? ` since ${ctx.baseline.label}` : "";
  const own = c.projection_change === null
    ? "his own projection change is unknown"
    : `his own projection ${moved(c.projection_change, "rose", "fell")}`;
  const pos = r.position ?? "position";
  const repl = c.replacement_change === null
    ? `the ${pos} replacement level change is unknown`
    : `the ${pos} replacement level ${moved(c.replacement_change, "rose", "fell")}`;
  return `${head}${since}: over ${weeksText(ctx.weeks)}, ${own} and ${repl}.`;
}

export function changeUnavailableText(ctx: ChangeContext | null): string {
  return ctx?.reason ?? "No earlier projection snapshot is stored, so no change is shown.";
}

/** Value header text: "Fantasy Intel model estimate · ESPN weekly projections · weeks 3–17 · calculation tv-1". */
export function basisLine(b: Basis): string {
  return [b.label, b.projection_label, weeksText(b.window.weeks), `calculation ${b.calculation_version}`].join(" · ");
}

export function selectedRow(rows: readonly ValueRow[], id: number | null): ValueRow | null {
  return id === null ? null : rows.find((r) => r.espn_player_id === id) ?? null;
}
