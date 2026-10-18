// Pure helpers for the /injury-impact page: URL state, the API path, the response types, grouping and labels.
// No React and no `@/` imports, so they run under the unit-test runner (node --experimental-strip-types).
// The contract (method, API, response shape) is docs/injury_impact.md.
import type { SearchParams } from "./sort.ts";

export type InjuryPosition = "ALL" | "QB" | "RB" | "WR" | "TE";
export type InjuryStatusFilter = "all" | "out" | "questionable";
export type InjuryLevel = "QUESTIONABLE" | "DOUBTFUL" | "OUT" | "IR" | "SUSPENDED";
export type Clarity = "no_history" | "thin_history" | "split" | "leaning";
export type FreshnessStatus = "fresh" | "stale" | "missing" | "post_kickoff_only";

export const INJURY_POSITIONS: readonly InjuryPosition[] = ["ALL", "QB", "RB", "WR", "TE"];
export const INJURY_STATUS_FILTERS: readonly InjuryStatusFilter[] = ["all", "out", "questionable"];

export type Roster = {
  status: "ROSTERED" | "FREE_AGENT" | "WAIVERS" | "UNKNOWN";
  fantasy_team_id: number | null;
  fantasy_team: string | null;
  is_mine: boolean;
  percent_owned: number | null;
};

export type UsageSide = {
  games: number;
  volume_per_game: number | null;
  snap_share: number | null;
  target_share: number | null;
  ppr_per_game: number | null;
};

export type Teammate = {
  gsis_id: string;
  espn_player_id: number | null;
  full_name: string;
  position: string | null;
  depth_rank: number | null;
  availability: string;
  healthy: boolean;
  recent_volume_per_game: number | null;
  observed: { with: UsageSide | null; without: UsageSide | null; delta_per_game: number | null };
  estimate: { share: number | null; extra_per_game: number | null; basis_volume_per_game: number | null; basis_weeks: number[] } | null;
  espn_projection: number | null;
  roster: Roster;
};

export type Receiver = {
  gsis_id: string;
  espn_player_id: number | null;
  full_name: string;
  position: string | null;
  availability: string;
  recent_targets_per_game: number;
  observed: { with: UsageSide | null; without: UsageSide | null; ppr_delta_per_game: number | null };
  espn_projection: number | null;
  roster: Roster;
};

export type WeekPoint = { week: number; played: boolean; snap_share: number | null; opportunities: number | null };

export type InjuryEntry = {
  gsis_id: string;
  espn_player_id: number | null;
  full_name: string;
  position: "QB" | "RB" | "WR" | "TE";
  team: string;
  status: {
    level: InjuryLevel;
    sources: string[];
    espn_injury_status: string | null;
    nfl_report_status: string | null;
    injury: string | null;
    practice_status: string | null;
  };
  absence: { kind: "new" | "ongoing" | "long_term"; last_played_week: number; consecutive_team_games_missed: number };
  workload: {
    games: number;
    team_games: number;
    last_played_week: number;
    snap_share: number | null;
    recent_snap_share: number | null;
    recent_weeks: number[];
    target_share: number | null;
    backfield_share: number | null;
    targets_per_game: number | null;
    carries_per_game: number | null;
    pass_attempts_per_game: number | null;
    xfp_per_game: number | null;
    ppr_per_game: number | null;
    role_trend: string | null;
    weekly: WeekPoint[];
  };
  roster: Roster;
  espn_projection: number | null;
  game: { week: number; opponent: string | null; is_home: boolean | null; kickoff: string | null; started: boolean; on_bye: boolean | null };
  freshness: {
    status: FreshnessStatus;
    reason: string;
    source_used: "nflverse" | "espn_status" | "none";
    evidence_retrieved_at: string | null;
    hours_since_retrieval: number | null;
  } | null;
  impact: {
    unit: "touches" | "targets" | "pass_attempts";
    unit_label: string;
    vacated_per_game: number | null;
    team_games_without: number;
    clarity: { label: Clarity; text: string };
    teammates: Teammate[];
    receivers: Receiver[];
  };
  affects_my_roster: boolean;
};

export type InjuryNotice = { tone: "info" | "caution" | "negative"; code: string; text: string };

export type InjuryImpactResponse = {
  season: number;
  week: number;
  position: InjuryPosition;
  status: InjuryStatusFilter;
  generated_at: string;
  league_context: { available: boolean; league_name: string | null; my_team_id: number | null; perspective: "query" | "owner_default" };
  freshness: {
    injury_report: { available: boolean; latest_week: number | null; current_week_rows: number };
    summary: { teams: number; by_status: Record<FreshnessStatus, number>; by_source: Record<string, number> };
    thresholds: { stale_after_hours: number; window_hours: number };
    espn_players_synced_at: string | null;
    espn_projections_synced_at: string | null;
    usage_through_week: number | null;
    depth_chart_as_of: string | null;
  };
  method: { observed: string; estimate: string; thresholds: Record<string, number> };
  counts: {
    total: number; matched: number; out: number; questionable: number; long_term: number; affects_my_roster: number;
    skipped: { no_games: number; small_role: number; no_team: number };
  };
  notices: InjuryNotice[];
  players: InjuryEntry[];
};

// ----------------------------------------------------------------------------- URL state

export type InjuryFilters = { pos: InjuryPosition; status: InjuryStatusFilter };

const first = (v: string | string[] | undefined): string | undefined => (Array.isArray(v) ? v[0] : v);

function oneOf<T extends string>(value: string | undefined, allowed: readonly T[], fallback: T): T {
  return value !== undefined && (allowed as readonly string[]).includes(value) ? (value as T) : fallback;
}

/** The page's filters from the URL; anything missing or invalid is the default (every position, every status). */
export function parseInjuryFilters(sp: SearchParams | undefined): InjuryFilters {
  return {
    pos: oneOf(first(sp?.pos)?.toUpperCase(), INJURY_POSITIONS, "ALL"),
    status: oneOf(first(sp?.status)?.toLowerCase(), INJURY_STATUS_FILTERS, "all"),
  };
}

export const OUT_LEVELS: readonly InjuryLevel[] = ["OUT", "DOUBTFUL", "IR", "SUSPENDED"];

/**
 * The entries a filter shows. The page reads the whole list once (the roster panel always covers every absence)
 * and filters here, with the same rules as the API's `position` / `status` parameters.
 */
export function filterEntries(entries: InjuryEntry[], f: InjuryFilters): InjuryEntry[] {
  return entries.filter((e) => (f.pos === "ALL" || e.position === f.pos)
    && (f.status === "all" || (f.status === "out") === OUT_LEVELS.includes(e.status.level)));
}

/** This page's URL with `patch` applied (null removes a key); every other param, the team included, is kept. */
export function injuryHref(sp: SearchParams | undefined, patch: Record<string, string | null>): string {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(sp ?? {})) {
    if (k in patch || v === undefined) continue;
    for (const item of Array.isArray(v) ? v : [v]) qs.append(k, item);
  }
  for (const [k, v] of Object.entries(patch)) if (v !== null) qs.set(k, v);
  const s = qs.toString();
  return s ? `?${s}` : "?";
}

// ----------------------------------------------------------------------------- grouping

export type EntryGroups = { upcoming: InjuryEntry[]; longTerm: InjuryEntry[]; done: InjuryEntry[] };

/**
 * Where an entry belongs on the page: games still to play first; a long-term absence (already in teammates' recent
 * usage) next; games already kicked off or on a bye last. The backend's order is kept inside each group.
 */
export function groupEntries(entries: InjuryEntry[]): EntryGroups {
  const out: EntryGroups = { upcoming: [], longTerm: [], done: [] };
  for (const e of entries) {
    if (e.game.started || e.game.on_bye) out.done.push(e);
    else if (e.absence.kind === "long_term") out.longTerm.push(e);
    else out.upcoming.push(e);
  }
  return out;
}

export type RosterAction = {
  kind: "my_injured" | "my_gainer" | "pickup";
  entry: InjuryEntry;
  mate: Teammate | null;
};

/** A teammate is listed as a possible gainer when the pro-rata estimate gives him a real share of real volume, or
 *  when he gained that much in observed games without the injured player. Per game, in the entry's unit. */
export const GAIN_MIN_SHARE = 0.25;
export const GAIN_MIN_EXTRA = 2;
export const GAIN_MIN_OBSERVED = 3;

function gains(m: Teammate): boolean {
  if (!m.healthy) return false;
  const est = (m.estimate?.share ?? 0) >= GAIN_MIN_SHARE && (m.estimate?.extra_per_game ?? 0) >= GAIN_MIN_EXTRA;
  return est || (m.observed.delta_per_game ?? 0) >= GAIN_MIN_OBSERVED;
}

/** Pickups are ordered by the larger of the estimated and the observed extra volume per game. */
function pickupWeight(m: Teammate): number {
  return Math.max(m.estimate?.extra_per_game ?? 0, m.observed.delta_per_game ?? 0);
}

/**
 * The decision list for the selected roster, over games still to play and absences that are not yet long-term:
 * your injured players (start/sit), your healthy teammates of an injured player who could gain, and available
 * teammates (free agent or waivers) who could gain. A player appears once per list.
 */
export function rosterActions(entries: InjuryEntry[]): { myInjured: RosterAction[]; myGainers: RosterAction[]; pickups: RosterAction[] } {
  const live = entries.filter((e) => !e.game.started && !e.game.on_bye && e.absence.kind !== "long_term");
  const myInjured = live.filter((e) => e.roster.is_mine).map((entry) => ({ kind: "my_injured" as const, entry, mate: null }));
  const seen = new Set<string>();
  const myGainers: RosterAction[] = [];
  const pickups: RosterAction[] = [];
  for (const entry of live) {
    if (entry.position === "QB") continue;   // a QB absence has no pro-rata split; receivers lose, the backup is listed
    for (const mate of entry.impact.teammates) {
      if (!gains(mate) || seen.has(mate.gsis_id)) continue;
      if (mate.roster.is_mine) {
        seen.add(mate.gsis_id);
        myGainers.push({ kind: "my_gainer", entry, mate });
      } else if (mate.roster.status === "FREE_AGENT" || mate.roster.status === "WAIVERS") {
        seen.add(mate.gsis_id);
        pickups.push({ kind: "pickup", entry, mate });
      }
    }
  }
  pickups.sort((a, b) => pickupWeight(b.mate as Teammate) - pickupWeight(a.mate as Teammate));
  return { myInjured, myGainers, pickups };
}

// ----------------------------------------------------------------------------- labels

const LEVEL_LABEL: Record<InjuryLevel, string> = {
  QUESTIONABLE: "Questionable", DOUBTFUL: "Doubtful", OUT: "Out", IR: "Injured reserve", SUSPENDED: "Suspended",
};

export function levelLabel(level: InjuryLevel): string {
  return LEVEL_LABEL[level] ?? level;
}

/** The Badge value for a level (the shared Badge knows these as availability letter codes). */
export function levelBadge(level: InjuryLevel | string): string {
  return level === "SUSPENDED" ? "SUSPENSION" : level;
}

/** "Missed the last 2 games (last played week 4)" / "Played the team's last game (week 6)". */
export function absenceLabel(a: InjuryEntry["absence"]): string {
  if (a.consecutive_team_games_missed === 0) return `Played the team's last game (week ${a.last_played_week})`;
  const n = a.consecutive_team_games_missed;
  return `Missed the last ${n === 1 ? "game" : `${n} games`} (last played week ${a.last_played_week})`;
}

export const CLARITY_LABEL: Record<Clarity, string> = {
  no_history: "No observed games without him",
  thin_history: "One game without him",
  split: "Split or unclear",
  leaning: "Leaning, small sample",
};

const FRESH_LABEL: Record<FreshnessStatus, string> = {
  fresh: "Fresh", stale: "Stale", missing: "No report", post_kickoff_only: "After kickoff",
};

/** The badge word (fresh, stale, no report…) and a detail line naming the source and the retrieval age. */
export function freshnessLabel(f: InjuryEntry["freshness"]): { text: string; detail: string | null; tone: "good" | "warn" | "bad" | "" } {
  if (!f) return { text: "Not on the slate", detail: null, tone: "" };
  const tone = f.status === "fresh" ? "good" : f.status === "missing" ? "bad" : f.status === "stale" ? "warn" : "";
  const src = f.source_used === "nflverse" ? "NFL report" : f.source_used === "espn_status" ? "ESPN status" : "No source";
  const age = f.hours_since_retrieval === null ? "" : ` · ${hoursAgo(f.hours_since_retrieval)}`;
  return { text: FRESH_LABEL[f.status], detail: `${src}${age}`, tone };
}

/** "4h ago", "2d ago", "under 1h ago". */
export function hoursAgo(h: number): string {
  if (h < 1) return "under 1h ago";
  if (h < 48) return `${Math.round(h)}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

/** "vs BUF", "@ BUF", "Bye", or null when unknown. */
export function gameLabel(g: InjuryEntry["game"]): string | null {
  if (g.on_bye) return "Bye";
  if (!g.opponent) return null;
  return g.is_home === true ? `vs ${g.opponent}` : g.is_home === false ? `@ ${g.opponent}` : g.opponent;
}

/** Roster state in words; colour only repeats it. */
export function rosterText(r: Roster): { text: string; tone: "info" | "good" | "warn" | "" } {
  if (r.is_mine) return { text: "Your team", tone: "info" };
  switch (r.status) {
    case "ROSTERED": return { text: r.fantasy_team ?? "Rostered", tone: "" };
    case "FREE_AGENT": return { text: "Free agent", tone: "good" };
    case "WAIVERS": return { text: "Waivers", tone: "warn" };
    default: return { text: "Unknown", tone: "" };
  }
}

/** The waivers page filtered to a position. */
export function waiversHref(position: string | null): string {
  return position && ["QB", "RB", "WR", "TE"].includes(position) ? `/waivers?pos=${position}` : "/waivers";
}

/** The healthy teammate with the largest estimated share (else the most recent volume), for the compare link. */
export function compareTarget(entry: InjuryEntry): Teammate | null {
  const healthy = entry.impact.teammates.filter((m) => m.healthy && m.espn_player_id !== null);
  return healthy[0] ?? null;
}

/** Snap share per team game, null where he did not play (a gap in the sparkline, never a zero). */
export function snapSeries(w: InjuryEntry["workload"]): (number | null)[] {
  return w.weekly.map((p) => (p.played ? p.snap_share : null));
}
