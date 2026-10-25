// Pure helpers for the game-splits page (issue #210): URL state, the backend query and display formatting.
// No React and no `@/` imports, so it runs under the unit-test runner. The backend computes every number.
import type { SplitsMetricFormat, SplitsResult, SplitsWeek } from "./game-splits-types.ts";

export type SplitsState = {
  player: string | null;
  teammate: string | null;
  seasonFrom: number | null;
  weekFrom: number | null;
  seasonTo: number | null;
  weekTo: number | null;
  /** YYYY-MM-DD: only games played before this date count. */
  asOf: string | null;
  /** Week drilldown filter. */
  show: "all" | "samples" | "excluded";
};

type Params = Record<string, string | string[] | undefined>;
const first = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);
const GSIS = /^[0-9A-Za-z-]{4,20}$/;
const DATE = /^\d{4}-\d{2}-\d{2}$/;

function intIn(v: string | undefined, lo: number, hi: number): number | null {
  if (v === undefined || !/^\d+$/.test(v)) return null;
  const n = Number(v);
  return n >= lo && n <= hi ? n : null;
}

function validDate(v: string | undefined): string | null {
  if (!v || !DATE.test(v)) return null;
  const d = new Date(`${v}T00:00:00Z`);
  return Number.isNaN(d.getTime()) || d.toISOString().slice(0, 10) !== v ? null : v;
}

/** Splits state from the URL. Anything malformed falls back to the default instead of erroring; the backend applies
 *  the window rules (order, at most three seasons) and answers 422 with the reason. */
/** The splits answer when no NFL usage is stored at all: a 422 asking for a season. That is no data, not a bad window. */
export function splitsUnavailable(status: number | undefined, error: string | undefined): boolean {
  return status === 422 && /season must be given/.test(error ?? "");
}

export function parseSplitsState(sp: Params): SplitsState {
  const player = first(sp.player);
  const teammate = first(sp.teammate);
  const show = first(sp.show);
  const p = player && GSIS.test(player) ? player : null;
  // The form carries the player its teammate list was built for (`for_player`): when the submitted player differs,
  // the teammate belonged to the previous player and is dropped, so the page goes back to choosing a teammate.
  const forPlayer = first(sp.for_player);
  const stale = forPlayer !== undefined && forPlayer !== "" && forPlayer !== p;
  return {
    player: p,
    teammate: p && !stale && teammate && GSIS.test(teammate) && teammate !== p ? teammate : null,
    seasonFrom: intIn(first(sp.season_from), 1999, 2100),
    weekFrom: intIn(first(sp.week_from), 1, 18),
    seasonTo: intIn(first(sp.season_to), 1999, 2100),
    weekTo: intIn(first(sp.week_to), 1, 18),
    asOf: validDate(first(sp.as_of)),
    show: show === "samples" || show === "excluded" ? show : "all",
  };
}

const QUERY_KEYS: [keyof SplitsState, string][] = [
  ["player", "player"], ["teammate", "teammate"], ["seasonFrom", "season_from"], ["weekFrom", "week_from"],
  ["seasonTo", "season_to"], ["weekTo", "week_to"], ["asOf", "as_of"],
];

/** Backend query for /api/research/game-splits: every input in the query string (the response cache keys on it). */
export function splitsApiQuery(s: SplitsState): string {
  const q = new URLSearchParams();
  for (const [k, name] of QUERY_KEYS) {
    const v = s[k];
    if (v !== null && v !== undefined && k !== "show") q.set(name, String(v));
  }
  return q.toString();
}

/** Link to the splits page with the state overridden by `over` (`null` clears a value). Changing the player clears
 *  the teammate unless one is given. */
export function splitsHref(s: SplitsState, over: Partial<SplitsState> = {}): string {
  const n = { ...s, ...over };
  if ("player" in over && over.player !== s.player && !("teammate" in over)) n.teammate = null;
  const q = new URLSearchParams(splitsApiQuery(n));
  if (n.show !== "all") q.set("show", n.show);
  const qs = q.toString();
  return `/research/splits${qs ? `?${qs}` : ""}`;
}

/** The effective window as a sentence a user can reproduce ("2025 week 1 to 2025 week 18, games before 2025-10-01"). */
export function windowSentence(r: Pick<SplitsResult, "window">): string {
  const { start, end, as_of } = r.window;
  const span = start.season === end.season
    ? `${start.season} weeks ${start.week}–${end.week}`
    : `${start.season} week ${start.week} to ${end.season} week ${end.week}`;
  return as_of ? `${span}, games played before ${as_of}` : span;
}

/** A display string, or null when the value is missing (render the muted dash). */
export function formatSplit(v: number | null | undefined, format: SplitsMetricFormat): string | null {
  if (v === null || v === undefined || !Number.isFinite(v)) return null;
  if (format === "pct") return `${(v * 100).toFixed(1)}%`;
  if (format === "signed") {
    const r = Number(v.toFixed(1));
    return r > 0 ? `+${r.toFixed(1)}` : r < 0 ? `−${Math.abs(r).toFixed(1)}` : "0.0";
  }
  return v.toFixed(1);
}

/** A difference (without minus with) in the metric's own unit: percentage points for shares. */
export function formatDifference(v: number | null | undefined, format: SplitsMetricFormat): string | null {
  if (v === null || v === undefined || !Number.isFinite(v)) return null;
  const x = format === "pct" ? v * 100 : v;
  const r = Number(x.toFixed(1));
  const s = r > 0 ? `+${r.toFixed(1)}` : r < 0 ? `−${Math.abs(r).toFixed(1)}` : "0.0";
  return format === "pct" ? `${s} pts` : s;
}

/** Drilldown rows for the filter: samples (with and without), excluded (unknown status), or all. */
export function visibleWeeks(weeks: readonly SplitsWeek[], show: SplitsState["show"]): SplitsWeek[] {
  if (show === "samples") return weeks.filter((w) => w.outcome === "with" || w.outcome === "without");
  if (show === "excluded") return weeks.filter((w) => w.outcome === "excluded");
  return [...weeks];
}

export const OUTCOME_LABEL: Record<SplitsWeek["outcome"], string> = {
  with: "With",
  without: "Without",
  excluded: "Excluded",
  not_counted: "Not a sample",
};

/** Reason counts as sorted [label, count] pairs (largest first, then label) for a compact list. */
export function reasonList(counts: Record<string, number>, labels: Record<string, string>): [string, number][] {
  return Object.entries(counts)
    .map(([k, n]) => [labels[k] ?? k, n] as [string, number])
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

/** Season options for the window selects: the stored seasons, newest first. */
export function seasonOptions(seasons: readonly number[] | undefined, current: number | null): number[] {
  const set = new Set(seasons ?? []);
  if (current !== null) set.add(current);
  return [...set].sort((a, b) => b - a);
}
