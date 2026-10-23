// Types and pure helpers for the strength-of-schedule heatmap (GET /api/schedule-intelligence/sos, contract
// "sos-heatmap.v1", issue #188). The backend owns every number: opponents, ranks, the neutral baseline, differences,
// bands and the row tallies. This module only reads the URL, builds links and formats what the backend sent; a
// missing value stays missing (never 0) and nothing here invents a grade.

import type { ProvenancePayload } from "./signal-provenance.ts";

export const CONTRACT = "sos-heatmap.v1";
export const PATH = "/schedule-intelligence/sos";
export const POSITIONS = ["QB", "RB", "WR", "TE"] as const;
export type SosPosition = (typeof POSITIONS)[number];
export const HORIZONS = ["ros", "playoffs"] as const;
export type SosHorizon = (typeof HORIZONS)[number];
export const VIEWS = ["teams", "roster", "free_agents"] as const;
export type SosView = (typeof VIEWS)[number];
export const SORT_KEYS = ["name", "permissive", "tougher", "avg", "graded"] as const;

export const HORIZON_LABEL: Record<SosHorizon, string> = { ros: "Rest of season", playoffs: "Fantasy playoffs" };
export const VIEW_LABEL: Record<SosView, string> = { teams: "NFL teams", roster: "Roster", free_agents: "Free agents" };

export type SosLabel = "permissive" | "middle" | "tougher" | "insufficient" | "unavailable" | "bye";
export type SosStatus = "scheduled" | "bye" | "schedule_missing" | "team_unknown";

export type SosCell = {
  week: number;
  phase: "regular" | "playoffs" | null;
  status: SosStatus;
  label: SosLabel;
  label_text: string;
  reason: string | null;
  opponent?: string;
  home?: boolean;
  kickoff_known?: boolean;
  /** Rank of the opponent on the position's primary metric (1 = most permissive); null when not graded. */
  rank?: number | null;
  ranked_defenses?: number;
  value?: number | null;
  baseline?: number | null;
  /** Opponent value minus the neutral baseline; null when not graded. */
  delta?: number | null;
  games?: number;
  plays?: number | null;
  through_week?: number | null;
  excluded_games?: number;
  /** WR and TE only: the position's target share against this defense, context only (never part of the grade). */
  context?: { metric: string; label: string; value: number | null; numerator: number | null; denominator: number | null; missing_reason: string | null };
};

export type SosSummary = {
  weeks: number;
  counts: Record<SosLabel, number>;
  graded_weeks: number;
  games: number;
  average_rank: number | null;
  average_delta: number | null;
};

export type SosPlayer = {
  espn_player_id: number;
  name: string | null;
  position: string;
  pro_team: string | null;
  injury_status: string | null;
  on_ir: boolean;
  fantasy_team_id: number | null;
  availability_status: string | null;
  percent_owned: number | null;
};

export type SosRow = {
  kind: "team" | "player";
  nfl_team: string | null;
  team_uncertain: boolean;
  player: SosPlayer | null;
  notes: string[];
  cells: SosCell[];
  summary: SosSummary;
};

export type SosBaseline = { week: number; value: number | null; ranked_defenses: number; through_week: number | null; reason: string | null };

export type SosHeatmap = {
  contract: string;
  league: { league_id: number; season: number; name: string | null; current_week: number; final_scoring_period: number; last_regular_season_week: number | null };
  query: { position: SosPosition; horizon: SosHorizon; view: SosView | "player"; team_id: number | null; nfl_team: string | null; player_id: number | null };
  team: { team_id: number; name: string | null; abbrev: string | null; is_mine: boolean } | null;
  teams: { team_id: number; name: string | null; abbrev: string | null; is_mine: boolean }[];
  nfl_teams: string[];
  position: SosPosition;
  /** `positions`: every position graded on this same metric (QB, WR and TE share pass defense). */
  metric: { key: string; label: string; unit: string; kind: "mean" | "rate"; numerator: string; denominator: string; definition: string; positions: SosPosition[] };
  context_metric: { key: string; label: string; definition: string } | null;
  horizon: {
    key: SosHorizon; label: string; weeks: number[]; known: boolean; reason: string | null; source: string;
    regular_season_through: number | null; playoff_weeks: number[] | null; final_scoring_period: number; first_actionable_week: number | null;
  };
  window: { espn_current_week: number; first_actionable_week: number | null; uncertain: boolean | null; warning: string | null; reason: string | null };
  baselines: SosBaseline[];
  rows: SosRow[];
  rows_total: number;
  rows_shown: number;
  free_agent_limit: number | null;
  free_agent_order: string | null;
  free_agent_order_keys: string[] | null;
  coverage: { cells: number; scheduled: number; graded: number };
  source: {
    name: string | null; available: boolean; reason: string | null; through_week: number | null;
    defense_weeks_loaded: number[]; schedule_available: boolean; weeks_without_schedule: number[];
  };
  schedule: { available: boolean; warnings: { code: string; message: string }[] };
  labels: Record<SosLabel, string>;
  label_rule: string;
  baseline_rule: string;
  summary_rule: string;
  min_games: number;
  min_ranked_defenses: number;
  notes: string[];
  provenance?: ProvenancePayload;
};

// ----------------------------------------------------------------------------- URL state

type SP = Record<string, string | string[] | undefined>;
const first = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);

export type SosState = {
  position: SosPosition;
  horizon: SosHorizon;
  view: SosView;
  /** Fantasy team for the roster view (`team`, the app-wide team parameter). */
  team: number | null;
  nfl: string | null;
  player: number | null;
};

const ID = /^\d{1,12}$/;
const NFL = /^[A-Za-z]{2,3}$/;

export function parseSosState(sp: SP): SosState {
  const pos = first(sp.pos)?.toUpperCase();
  const horizon = first(sp.horizon);
  const view = first(sp.view);
  const team = first(sp.team)?.trim();
  const nfl = first(sp.nfl)?.trim();
  const player = first(sp.player)?.trim();
  return {
    position: (POSITIONS as readonly string[]).includes(pos ?? "") ? (pos as SosPosition) : "QB",
    horizon: (HORIZONS as readonly string[]).includes(horizon ?? "") ? (horizon as SosHorizon) : "ros",
    view: (VIEWS as readonly string[]).includes(view ?? "") ? (view as SosView) : "teams",
    team: team && /^\d{1,6}$/.test(team) ? Number(team) : null,
    nfl: nfl && NFL.test(nfl) ? nfl.toUpperCase() : null,
    player: player && ID.test(player) ? Number(player) : null,
  };
}

/** The backend request for a state. The fantasy team goes along only for the roster view (or a player link). */
export function sosApiPath(s: SosState): string {
  const q = new URLSearchParams({ position: s.position, horizon: s.horizon, view: s.view });
  if (s.team !== null && s.view === "roster") q.set("team_id", String(s.team));
  if (s.nfl) q.set("nfl_team", s.nfl);
  if (s.player !== null) q.set("player_id", String(s.player));
  return `/api/schedule-intelligence/sos?${q}`;
}

/** A link to this page with some state changed. Sorting is kept; the focused player is cleared unless it is set, since
 *  he may not be in the new filter. Defaults are left out of the URL. */
export function sosHref(s: SosState, change: Partial<SosState>, sp: SP = {}): string {
  const next = { ...s, player: null, ...change };
  const q = new URLSearchParams();
  if (next.position !== "QB") q.set("pos", next.position);
  if (next.horizon !== "ros") q.set("horizon", next.horizon);
  if (next.view !== "teams") q.set("view", next.view);
  if (next.team !== null) q.set("team", String(next.team));
  if (next.nfl) q.set("nfl", next.nfl);
  if (next.player !== null) q.set("player", String(next.player));
  for (const k of ["sort", "dir"]) {
    const v = first(sp[k]);
    if (v) q.set(k, v);
  }
  const qs = q.toString();
  return qs ? `${PATH}?${qs}` : PATH;
}

// ----------------------------------------------------------------------------- formatting

/** Glyphs that carry the band beside the colour: a grade is never colour alone. */
export const GLYPH: Record<SosLabel, string> = {
  permissive: "▲", middle: "●", tougher: "▼", insufficient: "?", unavailable: "—", bye: "",
};

export const isGraded = (c: SosCell): boolean => c.rank !== undefined && c.rank !== null;

/** A metric value in its unit: EPA per play with sign and three decimals, a rate as a percentage. */
export function fmtValue(v: number | null | undefined, kind: "mean" | "rate"): string | null {
  if (v === null || v === undefined || Number.isNaN(v)) return null;
  if (kind === "rate") return `${(v * 100).toFixed(1)}%`;
  const t = Math.abs(v).toFixed(3);
  return Number(t) === 0 ? "0.000" : `${v > 0 ? "+" : "−"}${t}`;
}

/** The difference from the neutral baseline, compact for a cell: "+.052", "−.031" (EPA) or "+2.1 pts" (rate). */
export function fmtDelta(v: number | null | undefined, kind: "mean" | "rate"): string | null {
  if (v === null || v === undefined || Number.isNaN(v)) return null;
  if (kind === "rate") {
    const t = Math.abs(v * 100).toFixed(1);
    return Number(t) === 0 ? "0.0" : `${v > 0 ? "+" : "−"}${t}`;
  }
  const t = Math.abs(v).toFixed(3);
  if (Number(t) === 0) return ".000";
  return `${v > 0 ? "+" : "−"}${t.replace(/^0/, "")}`;
}

export const where = (c: SosCell): string => (c.home ? "vs" : "@");

/** A full sentence for one cell: tooltips and assistive technology read this, never colour. */
export function describeCell(c: SosCell, d: Pick<SosHeatmap, "metric" | "min_games">): string {
  const wk = `Week ${c.week}`;
  if (c.status === "bye") return `${wk}: bye.`;
  if (c.status !== "scheduled") return `${wk}: no opponent shown (${c.reason ?? "unknown"}).`;
  const opp = `${c.home ? "home vs" : "away at"} ${c.opponent}`;
  const sample = `${c.games ?? 0} presumed-complete ${c.games === 1 ? "game" : "games"}${c.plays ? `, ${c.plays} plays` : ""}`;
  if (!isGraded(c)) return `${wk}: ${opp}. Not graded: ${c.reason ?? "no evidence"}. Sample: ${sample}.${contextText(c)}`;
  const value = fmtValue(c.value, d.metric.kind);
  const delta = fmtDelta(c.delta, d.metric.kind);
  return `${wk}: ${opp}. ${c.label_text}: rank ${c.rank} of ${c.ranked_defenses} on ${d.metric.label}`
    + ` (${value}${delta ? `, ${delta} from the neutral baseline` : ""}). Sample: ${sample}.${contextText(c)}`;
}

/** The WR/TE target-share context, said to be context only; empty when the cell has none. */
export function contextText(c: SosCell): string {
  const x = c.context;
  if (!x) return "";
  if (x.value === null) return ` Context only, not part of the grade: ${x.label} unavailable (${x.missing_reason ?? "no data"}).`;
  return ` Context only, not part of the grade: ${x.label} ${(x.value * 100).toFixed(1)}%`
    + `${x.numerator !== null && x.denominator !== null ? ` (${x.numerator} of ${x.denominator} targets)` : ""}.`;
}

/** Which positions a grid's ranking applies to, in words: "the same ranking for QB, WR and TE" or "RB only". */
export function metricScope(d: Pick<SosHeatmap, "metric" | "position">): string {
  const ps = d.metric.positions?.length ? d.metric.positions : [d.position];
  if (ps.length === 1) return `${ps[0]} only`;
  return `the same ranking for ${ps.slice(0, -1).join(", ")} and ${ps[ps.length - 1]}`;
}

/** The short sample line of a cell that is not graded: the games behind it against the minimum. */
export function sampleText(c: SosCell, minGames: number): string | null {
  if (c.status !== "scheduled") return null;
  if (c.value === null || c.value === undefined) return "no data";
  return (c.games ?? 0) < minGames ? `${c.games ?? 0} of ${minGames} g` : `${c.games} g`;
}

export function weeksText(weeks: number[]): string {
  if (weeks.length === 0) return "no weeks";
  if (weeks.length === 1) return `week ${weeks[0]}`;
  const contiguous = weeks.every((w, i) => i === 0 || w === weeks[i - 1] + 1);
  return contiguous ? `weeks ${weeks[0]}–${weeks[weeks.length - 1]}` : `weeks ${weeks.join(", ")}`;
}

export function rowName(r: SosRow): string {
  if (r.player) return r.player.name ?? `Player #${r.player.espn_player_id}`;
  return r.nfl_team ?? "Unknown team";
}

// ----------------------------------------------------------------------------- sorting

export type SortKey = (typeof SORT_KEYS)[number];

/** Weeks in a band out of the graded weeks, or null when no week was graded (the count is unknown, not 0). */
export function tally(r: SosRow, band: "permissive" | "tougher"): { count: number; graded: number } | null {
  if (r.summary.graded_weeks === 0) return null;
  return { count: r.summary.counts[band], graded: r.summary.graded_weeks };
}

/** The value a row sorts by. Missing averages stay missing (sorted last), never 0. */
export function sortValue(r: SosRow, key: string): string | number | null {
  switch (key) {
    // A row with no graded week has unknown counts, not zero: it sorts last in both directions.
    case "permissive": return tally(r, "permissive")?.count ?? null;
    case "tougher": return tally(r, "tougher")?.count ?? null;
    case "avg": return r.summary.average_rank;
    case "graded": return r.summary.graded_weeks;
    default: return rowName(r);
  }
}

// ----------------------------------------------------------------------------- links

/** Decision links for a row, all in-app paths (the page wraps them in league-scoped links). */
export type RowLinks = {
  profile: string | null;
  /** Waivers: the add/drop scenario for an available player, else the position's waiver list. */
  waivers: { href: string; label: string } | null;
  /** Trades: the analyzer for a rostered player, else the position's trade-values board. */
  trade: { href: string; label: string } | null;
};

export function rowLinks(r: SosRow, position: SosPosition, myTeamId: number | null): RowLinks {
  const p = r.player;
  if (!p) {
    return {
      profile: null,
      waivers: { href: `/waivers?pos=${position}`, label: `${position} waivers` },
      trade: { href: `/trades/values?pos=${position}`, label: `${position} trade values` },
    };
  }
  const profile = `/players?id=${p.espn_player_id}`;
  if (p.fantasy_team_id === null) {
    const available = p.availability_status === "FREEAGENT" || p.availability_status === "WAIVERS";
    return {
      profile,
      waivers: available ? { href: `/waivers?add=${p.espn_player_id}#add-drop`, label: "Add/drop" } : { href: `/waivers?pos=${position}`, label: `${position} waivers` },
      trade: null,
    };
  }
  // With no viewer team the analyzer opens on the owner's side, under a neutral label (never "Trade away").
  if (myTeamId === null) {
    return { profile, waivers: null, trade: { href: `/trades?ta=${p.fantasy_team_id}&a=${p.espn_player_id}`, label: "Trade analyzer" } };
  }
  const own = p.fantasy_team_id === myTeamId;
  const href = own ? `/trades?ta=${p.fantasy_team_id}&a=${p.espn_player_id}`
    : `/trades?ta=${myTeamId}&tb=${p.fantasy_team_id}&b=${p.espn_player_id}`;
  return { profile, waivers: null, trade: { href, label: own ? "Trade away" : "Trade for" } };
}
