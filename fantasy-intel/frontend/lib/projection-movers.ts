// Types and pure helpers for the Projection movers page (GET /api/projections/movers, contract
// "projection-movers/v1"). Every number, rank and evidence signal comes from the backend; these helpers
// validate the URL, select, order and phrase those values. They never recompute a projection, a delta or a
// rank, and they never decide what caused a change.
import type { ProjectionGovernance } from "./projection-source.ts";
import type { SortState } from "./sort.ts";

export const CONTRACT = "projection-movers/v1";

export const POSITIONS = ["QB", "RB", "WR", "TE"] as const;
export type Position = (typeof POSITIONS)[number];

export type MoverSource = "espn" | "independent";
export type MoverView = "ros" | "week";

export const DEFAULT_LIMIT = 15;

// ----------------------------------------------------------------------------- API contract

export type SnapshotRef = { id: string; label: string; taken_at: string | null; cycle_week: number | null };
export type SnapshotDetail = SnapshotRef & {
  model_version: string | null;
  run_ids: Record<string, string> | string[] | null;
};

export type EvidenceKind = "usage" | "availability" | "role" | "schedule" | "model";
export type EvidenceUnit = "share" | "count" | "points" | "text";

export type EvidenceSignal = {
  kind: EvidenceKind;
  key: string;
  label: string;
  before: number | string | null;
  after: number | string | null;
  unit: EvidenceUnit;
  weeks_before: number[];
  weeks_after: number[];
  source: string;
  detail: string | null;
};

export type Evidence = {
  headline: string | null;
  signals: EvidenceSignal[];
  missing: string[];
  information_window: {
    from: string | null;
    to: string | null;
    games: { week: number; team: string; kickoff: string | null }[];
  } | null;
  note: string;
};

export type MoverWeek = {
  week: number;
  before: number | null;
  after: number | null;
  delta: number | null;
  bye: boolean;
  status_before: string | null;
  status_after: string | null;
  horizon_before: number | null;
  horizon_after: number | null;
};

export type Mover = {
  espn_player_id: number;
  name: string;
  position: Position;
  nfl_team: string | null;
  gsis_id: string | null;
  before: number;
  after: number;
  delta: number;
  pct: number | null;
  per_week_before: number;
  per_week_after: number;
  rank_before: number | null;
  rank_after: number | null;
  rank_change: number | null;
  direction: "up" | "down" | "flat";
  relevant: boolean;
  weeks: MoverWeek[];
  components: { availability: number; per_game: number } | null;
  evidence: Evidence | null;
};

export type Thresholds = { min_delta: number; relevant_per_week: number; pct_floor: number; flat: number };

export type MoversResponse = {
  contract: string;
  season: number;
  source: MoverSource;
  view: MoverView;
  position: Position | null;
  available: boolean;
  reason: string | null;
  label: string;
  experimental: boolean;
  notes: string[];
  governance: ProjectionGovernance | null;
  baselines: SnapshotRef[];
  baseline: SnapshotDetail | null;
  current: SnapshotDetail | null;
  window: {
    weeks: number[];
    excluded_weeks: { week: number; reason: string }[];
    scoring_verified: boolean;
    scoring_note: string | null;
  };
  summary: {
    compared: number;
    risers: number;
    fallers: number;
    unchanged: number;
    not_comparable: number;
    not_comparable_reasons: Record<string, number>;
  };
  skipped: { run_id: string; week: number | null; reason: string }[];
  excluded_runs: { run_id: string; week: number | null; reason: string }[];
  risers: Mover[];
  fallers: Mover[];
  players: Mover[];
  thresholds: Thresholds;
};

export const DEFAULT_THRESHOLDS: Thresholds = { min_delta: 0.5, relevant_per_week: 3.0, pct_floor: 1.0, flat: 0.05 };

// ----------------------------------------------------------------------------- URL state

export type MoversState = {
  source: MoverSource;
  view: MoverView;
  position: Position | null;
  baseline: string | null;
  player: number | null;
};

const first = (v: string | string[] | undefined): string | undefined => (Array.isArray(v) ? v[0] : v);

/** A snapshot id as the backend issues it: an ESPN date ("2026-10-14") or an own-model cutoff ("w6"). */
export const BASELINE_PATTERN = /^(?:\d{4}-\d{2}-\d{2}|w\d{1,2})$/;

/**
 * The page state from the URL. Anything unknown falls back to the default (ESPN, rest of season, all
 * positions, the backend's default baseline, the top riser), so a stale or hand-edited link never reaches
 * the API with a value it would reject. `source=ours` is accepted as an alias of `independent`.
 */
export function parseMoversState(sp: Record<string, string | string[] | undefined> | undefined): MoversState {
  const s = first(sp?.source);
  const v = first(sp?.view);
  const p = first(sp?.position)?.toUpperCase();
  const b = first(sp?.baseline);
  const id = first(sp?.player);
  return {
    source: s === "independent" || s === "ours" ? "independent" : "espn",
    view: v === "week" ? "week" : "ros",
    position: (POSITIONS as readonly string[]).includes(p ?? "") ? (p as Position) : null,
    baseline: b && BASELINE_PATTERN.test(b) ? b : null,
    player: id && /^\d{1,12}$/.test(id) && Number(id) > 0 ? Number(id) : null,
  };
}

/** The API query string for a state. Source and view are always sent; the rest only when chosen. */
export function moversQuery(state: MoversState, limit = DEFAULT_LIMIT): string {
  const qs = new URLSearchParams({ source: state.source, view: state.view, limit: String(limit) });
  if (state.position) qs.set("position", state.position);
  if (state.baseline) qs.set("baseline", state.baseline);
  return qs.toString();
}

/**
 * The page URL for a state with `updates` applied. Defaults are left out so the plain page keeps a plain URL.
 * Changing anything but the player clears the player, because the selected row may not exist in the new view.
 */
export function moversHref(state: MoversState, updates: Partial<MoversState> = {}): string {
  const next: MoversState = { ...state, ...updates };
  if (!("player" in updates)) next.player = null;
  const qs = new URLSearchParams();
  if (next.source !== "espn") qs.set("source", next.source);
  if (next.view !== "ros") qs.set("view", next.view);
  if (next.position) qs.set("position", next.position);
  if (next.baseline) qs.set("baseline", next.baseline);
  if (next.player) qs.set("player", String(next.player));
  const q = qs.toString();
  return q ? `/movers?${q}` : "/movers";
}

// ----------------------------------------------------------------------------- wording

/** [7, 8, 9, 11] as "7–9, 11": runs of two or more consecutive weeks collapse to a range. */
export function weekSpan(weeks: readonly number[]): string {
  const sorted = [...weeks].sort((a, b) => a - b);
  const parts: string[] = [];
  for (let i = 0; i < sorted.length; ) {
    let j = i;
    while (j + 1 < sorted.length && sorted[j + 1] === sorted[j] + 1) j++;
    parts.push(j > i ? `${sorted[i]}–${sorted[j]}` : String(sorted[i]));
    i = j + 1;
  }
  return parts.join(", ");
}

export const SOURCE_NAME: Record<MoverSource, string> = { espn: "ESPN projections", independent: "Fantasy Intel projections" };
export const VIEW_NAME: Record<MoverView, string> = { ros: "Rest of season", week: "Next week" };

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** A snapshot as people say it: "Oct 15" for an ESPN date, "week 6" for an own-model cutoff, else the backend label. */
export function snapshotName(snap: SnapshotRef): string {
  const d = /^\d{4}-(\d{2})-(\d{2})$/.exec(snap.id);
  if (d) {
    const month = MONTHS[Number(d[1]) - 1];
    if (month) return `${month} ${Number(d[2])}`;
  }
  const w = /^w(\d+)$/.exec(snap.id);
  if (w) return `week ${Number(w[1])}`;
  return snap.label;
}

/** "weeks 7–17", "week 7 only", or null for an empty window. */
export function windowText(weeks: readonly number[]): string | null {
  if (weeks.length === 0) return null;
  if (weeks.length === 1) return `week ${weeks[0]} only`;
  return `weeks ${weekSpan(weeks)}`;
}

/** The freshness line under the title: which two stored forecasts are compared and over which weeks. */
export function freshnessText(d: MoversResponse): string | null {
  if (!d.available || !d.current || !d.baseline) return null;
  const cur = snapshotName(d.current);
  const base = snapshotName(d.baseline);
  const span = windowText(d.window.weeks);
  const lead = d.source === "independent" ? "Fantasy Intel forecast from" : "ESPN snapshot";
  return `${lead} ${cur} vs ${base}${span ? ` · ${span}` : ""}`;
}

/** What the before and after numbers are: "Projected points summed over weeks 7–17" or "Projected points for week 8". */
export function totalsText(weeks: readonly number[]): string {
  if (weeks.length === 0) return "Projected points";
  if (weeks.length === 1) return `Projected points for week ${weeks[0]}`;
  return `Projected points summed over weeks ${weekSpan(weeks)}`;
}

/** The eyebrow: the NFL week the latest snapshot was made for, when the backend knows it. */
export function eyebrowText(d: MoversResponse | null): string {
  const w = d?.current?.cycle_week;
  return w ? `Projections · week ${w}` : "Projections";
}

/** "WR14 → WR9", "WR14, no change", or null when either rank is missing. */
export function rankText(m: Pick<Mover, "position" | "rank_before" | "rank_after">): string | null {
  if (m.rank_before === null || m.rank_after === null) return null;
  if (m.rank_before === m.rank_after) return `${m.position}${m.rank_after}, no change`;
  return `${m.position}${m.rank_before} → ${m.position}${m.rank_after}`;
}

export function rankTitle(m: Pick<Mover, "position" | "rank_before" | "rank_after" | "rank_change">): string {
  if (m.rank_before === null || m.rank_after === null || m.rank_change === null) {
    return `No ${m.position} rank: the player was not ranked in both snapshots`;
  }
  const n = Math.abs(m.rank_change);
  const places = `${n} ${n === 1 ? "place" : "places"}`;
  const how = m.rank_change > 0 ? `Moved up ${places}` : m.rank_change < 0 ? `Moved down ${places}` : "Same rank";
  return `${how} among ${m.position}s by window total (1 is the highest)`;
}

/** A signed change with a real minus sign and one decimal: "+11.5", "−4.2", "0.0". */
export function signedPts(v: number | null | undefined, digits = 1): string | null {
  if (v === null || v === undefined || Number.isNaN(v)) return null;
  const s = Math.abs(v).toFixed(digits);
  if (Number(s) === 0) return (0).toFixed(digits);
  return `${v > 0 ? "+" : "−"}${s}`;
}

/** A percent change, "+9.6%", or null when the backend gave none (the earlier total was below the floor). */
export function pctText(p: number | null | undefined): string | null {
  if (p === null || p === undefined || Number.isNaN(p)) return null;
  const s = Math.abs(p * 100).toFixed(1);
  if (Number(s) === 0) return "0.0%";
  return `${p > 0 ? "+" : "−"}${s}%`;
}

export const PCT_MISSING = "No percent change: the earlier total is too small for a percentage to mean anything";

/** One line for a mover: "120.4 → 131.9". */
export function beforeAfterText(m: Pick<Mover, "before" | "after">): string {
  return `${m.before.toFixed(1)} → ${m.after.toFixed(1)}`;
}

/** Identical reason sentences counted once each, in first-seen order. */
export function reasonCounts(items: readonly { reason: string }[]): { reason: string; n: number }[] {
  const counts = new Map<string, number>();
  for (const it of items) counts.set(it.reason, (counts.get(it.reason) ?? 0) + 1);
  return [...counts].map(([reason, n]) => ({ reason, n }));
}

/** Why some players were not compared, largest group first, capped at `max` lines. */
export function notComparableLines(summary: MoversResponse["summary"], max = 4): { lines: { reason: string; n: number }[]; more: number } {
  const all = Object.entries(summary.not_comparable_reasons)
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([reason, n]) => ({ reason, n }));
  return { lines: all.slice(0, max), more: Math.max(0, all.length - max) };
}

// ----------------------------------------------------------------------------- selection

/** The biggest riser and faller the backend ranked, or null when a list is empty. */
export function biggestMovers(d: MoversResponse): { riser: Mover | null; faller: Mover | null } {
  return { riser: d.risers[0] ?? null, faller: d.fallers[0] ?? null };
}

/**
 * The player whose detail is shown: the requested one when he is in the response, else the top riser, else
 * the top faller, else the largest change. Risers and fallers come first in the search because only they
 * carry evidence. `requestedMissing` is true when a player was asked for and is not in this comparison.
 */
export function selectedMover(d: MoversResponse, playerId: number | null): { mover: Mover | null; requestedMissing: boolean } {
  if (playerId !== null) {
    const hit = [...d.risers, ...d.fallers, ...d.players].find((m) => m.espn_player_id === playerId);
    if (hit) return { mover: hit, requestedMissing: false };
  }
  return { mover: d.risers[0] ?? d.fallers[0] ?? d.players[0] ?? null, requestedMissing: playerId !== null };
}

export type ChartMover = { id: number; label: string; value: number };

/** The top movers for the diverging bar chart: the biggest risers then the biggest fallers, largest change first. */
export function chartMovers(d: MoversResponse, each = 8): ChartMover[] {
  const seen = new Set<number>();
  const out: ChartMover[] = [];
  for (const m of [...d.risers.slice(0, each), ...d.fallers.slice(0, each)]) {
    if (seen.has(m.espn_player_id)) continue;
    seen.add(m.espn_player_id);
    out.push({ id: m.espn_player_id, label: m.name, value: m.delta });
  }
  return out.sort((a, b) => b.value - a.value);
}

export type WeekSeries = { labels: string[]; before: (number | null)[]; after: (number | null)[] };

/** The per-week lines for one player. A bye week is a gap, not a projected zero. */
export function weekSeries(m: Pick<Mover, "weeks">): WeekSeries {
  const weeks = [...m.weeks].sort((a, b) => a.week - b.week);
  return {
    labels: weeks.map((w) => String(w.week)),
    before: weeks.map((w) => (w.bye ? null : w.before)),
    after: weeks.map((w) => (w.bye ? null : w.after)),
  };
}

/** A short note for a week row when something other than a plain number sits behind it. */
export function weekNote(w: MoverWeek): string | null {
  if (w.bye) return "Bye week";
  const parts: string[] = [];
  const unusual = (s: string | null) => (s && s !== "projected" ? s.replaceAll("_", " ") : null);
  const b = unusual(w.status_before);
  const a = unusual(w.status_after);
  if (b || a) parts.push(b === a ? `Both: ${a}` : `Status: ${b ?? "projected"} → ${a ?? "projected"}`);
  if (w.horizon_before !== null || w.horizon_after !== null) {
    const h = (v: number | null) => (v === null ? "—" : v === 0 ? "next-week forecast" : `${v} ${v === 1 ? "week" : "weeks"} ahead`);
    parts.push(`Made ${h(w.horizon_before)} → ${h(w.horizon_after)}`);
  }
  return parts.length ? parts.join(" · ") : null;
}

// ----------------------------------------------------------------------------- evidence

export const GROUP_TITLE = { recorded: "Recorded data", model: "Model components" } as const;

/** Signals split for display: what the data recorded, and what the model's own parts did (ours only). */
export function groupSignals(e: Evidence | null | undefined): { recorded: EvidenceSignal[]; model: EvidenceSignal[] } {
  const out = { recorded: [] as EvidenceSignal[], model: [] as EvidenceSignal[] };
  for (const s of e?.signals ?? []) {
    if (s.kind === "model") out.model.push(s);
    else out.recorded.push(s); // an unknown kind is still recorded data, never silently dropped
  }
  return out;
}

/** One signal value for display, by unit: share 0.62 is "62%", count and points keep one decimal at most. */
export function signalValue(v: number | string | null, unit: EvidenceUnit): string | null {
  if (v === null || v === undefined) return null;
  if (typeof v === "string") return v;
  if (Number.isNaN(v)) return null;
  if (unit === "share") return `${Math.round(v * 100)}%`;
  if (unit === "points") return v.toFixed(1);
  return String(Number(v.toFixed(1)));
}

/** "weeks 3–5 → week 6": the sample behind a before and after value. Empty when no weeks are given. */
export function signalSample(s: Pick<EvidenceSignal, "weeks_before" | "weeks_after">): string {
  const one = (w: readonly number[]) => (w.length === 0 ? null : `${w.length === 1 ? "week" : "weeks"} ${weekSpan(w)}`);
  const b = one(s.weeks_before);
  const a = one(s.weeks_after);
  if (b && a) return `${b} → ${a}`;
  return b ?? a ?? "";
}

/** The window of games the evidence was read from: "Oct 14 – Oct 15 · week 6: KC, BUF" or null. */
export function informationWindowText(e: Evidence | null | undefined): string | null {
  const w = e?.information_window;
  if (!w) return null;
  const day = (iso: string | null) => {
    const m = iso ? /^(\d{4})-(\d{2})-(\d{2})/.exec(iso) : null;
    return m ? `${MONTHS[Number(m[2]) - 1]} ${Number(m[3])}` : null;
  };
  const from = day(w.from);
  const to = day(w.to);
  const span = from && to ? `${from} – ${to}` : from ?? to;
  const byWeek = new Map<number, string[]>();
  for (const g of w.games) byWeek.set(g.week, [...(byWeek.get(g.week) ?? []), g.team]);
  const games = [...byWeek].sort((a, b) => a[0] - b[0]).map(([wk, teams]) => `week ${wk}: ${teams.join(", ")}`).join("; ");
  const text = [span, games].filter(Boolean).join(" · ");
  return text || null;
}

// ----------------------------------------------------------------------------- table

export const SORT_KEYS = ["size", "player", "before", "after", "delta", "pct", "rank"] as const;
export const DEFAULT_SORT: SortState = { key: "size", dir: "desc" };

/** The value a table column sorts by. The default key is the size of the change, whichever way it points. */
export function moverSortValue(m: Mover, key: string): number | string | null {
  switch (key) {
    case "player": return m.name;
    case "before": return m.before;
    case "after": return m.after;
    case "delta": return m.delta;
    case "pct": return m.pct;
    case "rank": return m.rank_change;
    default: return Math.abs(m.delta);
  }
}
