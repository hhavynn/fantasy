// Pure helpers for the Fantasy Research Lab: URL state, position configs (charts, columns),
// formatting and action links. No React and no `@/` imports, so it runs under the unit-test runner.
// The backend computes every metric; this file only chooses, formats and links them.
import type { OpportunityResponse, ResearchPlayer, ResearchPosition, ResearchWindow, WindowSummary } from "./research-types.ts";

export type Slug = "wr" | "rb" | "te";
export const SLUGS: readonly Slug[] = ["wr", "rb", "te"];
export const POSITION_OF: Record<Slug, ResearchPosition> = { wr: "WR", rb: "RB", te: "TE" };
export const POSITION_NAME: Record<ResearchPosition, string> = { WR: "Wide receivers", RB: "Running backs", TE: "Tight ends" };

export type View = "opportunity" | "workload" | "momentum";
export const VIEWS: readonly View[] = ["opportunity", "workload", "momentum"];
export type YAxis = "fpoe" | "ppr";
export const WINDOWS: readonly ResearchWindow[] = ["season", "last5", "last3"];
export const WINDOW_SIZE: Record<ResearchWindow, number | null> = { season: null, last5: 5, last3: 3 };

export type ResearchState = {
  window: ResearchWindow;
  /** null = the position's default (the backend fills it in). */
  minGames: number | null;
  minVolume: number | null;
  team: string | null;
  view: View;
  y: YAxis;
  /** Selected and compared player (nflverse gsis id). */
  sel: string | null;
  cmp: string | null;
  sort: string | null;
  dir: "asc" | "desc" | null;
};

type Params = Record<string, string | string[] | undefined>;
const first = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);
const GSIS = /^[0-9A-Za-z-]{4,20}$/;

function intIn(v: string | undefined, lo: number, hi: number): number | null {
  if (v === undefined || !/^\d+$/.test(v)) return null;
  const n = Number(v);
  return n >= lo && n <= hi ? n : null;
}

function numIn(v: string | undefined, lo: number, hi: number): number | null {
  if (v === undefined || !/^\d+(\.\d+)?$/.test(v)) return null;
  const n = Number(v);
  return n >= lo && n <= hi ? n : null;
}

/** Research state from the URL. Anything invalid falls back to the default instead of erroring. */
export function parseResearchState(sp: Params): ResearchState {
  const w = first(sp.window);
  const window: ResearchWindow = w === "last3" || w === "last5" ? w : "season";
  let minGames = intIn(first(sp.games), 1, 18);
  const cap = WINDOW_SIZE[window];
  if (minGames !== null && cap !== null && minGames > cap) minGames = cap;
  const team = first(sp.team)?.toUpperCase();
  const view = first(sp.view);
  const sel = first(sp.sel);
  const cmp = first(sp.cmp);
  const dir = first(sp.dir);
  const sort = first(sp.sort);
  return {
    window,
    minGames,
    minVolume: numIn(first(sp.vol), 0, 40),
    team: team && /^[A-Z]{2,3}$/.test(team) ? team : null,
    view: view === "workload" || view === "momentum" ? view : "opportunity",
    y: first(sp.y) === "ppr" ? "ppr" : "fpoe",
    sel: sel && GSIS.test(sel) ? sel : null,
    cmp: cmp && GSIS.test(cmp) && cmp !== sel ? cmp : null,
    sort: sort && /^[a-z_]{1,32}$/.test(sort) ? sort : null,
    dir: dir === "asc" || dir === "desc" ? dir : null,
  };
}

/** Backend query for /api/research/opportunity. */
export function apiQuery(position: ResearchPosition, s: Pick<ResearchState, "window" | "minGames" | "minVolume" | "team">): string {
  const q = new URLSearchParams({ position, window: s.window });
  if (s.minGames !== null) q.set("min_games", String(s.minGames));
  if (s.minVolume !== null) q.set("min_volume", String(s.minVolume));
  if (s.team) q.set("team", s.team);
  return q.toString();
}

/** Link to a research page with the current state overridden by `over` (`null` clears a value). */
export function researchHref(slug: Slug, s: ResearchState, over: Partial<ResearchState> = {}): string {
  const n = { ...s, ...over };
  const q = new URLSearchParams();
  if (n.window !== "season") q.set("window", n.window);
  if (n.minGames !== null) q.set("games", String(n.minGames));
  if (n.minVolume !== null) q.set("vol", String(n.minVolume));
  if (n.team) q.set("team", n.team);
  if (n.view !== "opportunity") q.set("view", n.view);
  if (n.y !== "fpoe") q.set("y", n.y);
  if (n.sel) q.set("sel", n.sel);
  if (n.cmp && n.cmp !== n.sel) q.set("cmp", n.cmp);
  if (n.sort) q.set("sort", n.sort);
  if (n.sort && n.dir) q.set("dir", n.dir);
  const qs = q.toString();
  return `/research/${slug}${qs ? `?${qs}` : ""}`;
}

/** Plain search params for SortableTh (which rewrites only `sort` and `dir`). */
export function stateParams(s: ResearchState): Record<string, string> {
  const href = researchHref("wr", { ...s, sort: null, dir: null });
  const qs = href.includes("?") ? href.slice(href.indexOf("?") + 1) : "";
  return Object.fromEntries(new URLSearchParams(qs));
}

export const VOLUME_OPTIONS: Record<ResearchPosition, number[]> = {
  WR: [0, 2, 3, 4, 5, 6, 7, 8, 10],
  TE: [0, 1, 2, 3, 4, 5, 6, 7],
  RB: [0, 4, 6, 8, 10, 12, 15, 18],
};

export function gamesOptions(window: ResearchWindow): number[] {
  const cap = WINDOW_SIZE[window] ?? 8;
  return Array.from({ length: cap }, (_, i) => i + 1);
}

// ------------------------------------------------------------------ formatting --

export type Format = "pts" | "signed" | "pct" | "num1" | "num2";

/** A display string, or null when the value is missing (render as the muted dash). */
export function formatMetric(v: number | null | undefined, format: Format): string | null {
  if (v === null || v === undefined || !Number.isFinite(v)) return null;
  switch (format) {
    case "pct": return `${(v * 100).toFixed(1)}%`;
    case "signed": {
      const r = Number(v.toFixed(1));
      return r > 0 ? `+${r.toFixed(1)}` : r < 0 ? `−${Math.abs(r).toFixed(1)}` : "0.0";
    }
    case "num2": return v.toFixed(2);
    default: return v.toFixed(1);
  }
}

/** "Justin Jefferson" -> "J. Jefferson", "Amon-Ra St. Brown" -> "A. St. Brown", suffixes dropped. */
export function shortName(name: string): string {
  const words = name.trim().split(/\s+/).filter((w) => !/^(jr\.?|sr\.?|ii|iii|iv|v)$/i.test(w));
  if (words.length < 2) return name.trim();
  return `${words[0][0]}. ${words.slice(1).join(" ")}`;
}

/** Heat bucket 1..4 from a 0-100 position percentile; null (no tint) when the value is missing. */
export function heatBucket(percentile: number | null | undefined): 1 | 2 | 3 | 4 | null {
  if (percentile === null || percentile === undefined || !Number.isFinite(percentile)) return null;
  return percentile >= 90 ? 4 : percentile >= 75 ? 3 : percentile >= 50 ? 2 : 1;
}

export const HEAT_LEGEND = [
  { bucket: 4, label: "90th+" },
  { bucket: 3, label: "75th–89th" },
  { bucket: 2, label: "50th–74th" },
  { bucket: 1, label: "Below 50th" },
] as const;

/** Symmetric axis around zero so above and below expectation read at the same scale. */
export function symmetricDomain(values: readonly number[], floor = 4): [number, number] {
  const m = Math.max(floor, ...values.filter(Number.isFinite).map(Math.abs));
  const hi = Math.ceil(m / 2) * 2;
  return [-hi, hi];
}

/** 0 to the next round step above the data (and above `include`, e.g. a reference line). */
export function zeroDomain(values: readonly number[], step: number, include = 0): [number, number] {
  const m = Math.max(include, ...values.filter(Number.isFinite));
  return [0, Math.max(step, Math.ceil((m + step * 0.15) / step) * step)];
}

// --------------------------------------------------------------- configs --

export type MetricKey = keyof WindowSummary;
export type Column = { key: MetricKey | "momentum" | "trend" | "signal" | "games"; label: string; title: string; format: Format; heat?: boolean };

const SHARED_HEAD: Column[] = [
  { key: "games", label: "G", title: "Qualifying games in the window (played, with nflverse xFP and PPR)", format: "num1" },
  { key: "xfp_pg", label: "xFP/G", title: "Expected PPR points per game from the role (nflverse ffopportunity)", format: "pts", heat: true },
  { key: "ppr_pg", label: "PPR/G", title: "Actual nflverse PPR points per game over the same games", format: "pts", heat: true },
  { key: "fpoe_pg", label: "FPOE/G", title: "Mean of (PPR − xFP) over the same games", format: "signed", heat: true },
  { key: "momentum", label: "Δ xFP/G", title: "Recent 2–3 games minus the up-to-5 games before (season, not the window)", format: "signed" },
  { key: "trend", label: "Role", title: "Existing composite role trend from player scores", format: "num1" },
];
const SHARED_TAIL: Column[] = [
  { key: "snap_share", label: "Snap %", title: "Average share of team offensive snaps", format: "pct", heat: true },
  { key: "signal", label: "Signal", title: "Existing conservative market signal (season); a research prompt, not a recommendation", format: "num1" },
];

export const COLUMNS: Record<ResearchPosition, Column[]> = {
  WR: [
    ...SHARED_HEAD,
    { key: "target_share", label: "Tgt %", title: "Targets / team targets over the window", format: "pct", heat: true },
    { key: "air_yards_share", label: "Air %", title: "Air yards / team air yards over the window", format: "pct", heat: true },
    { key: "wopr", label: "WOPR", title: "1.5 × target share + 0.7 × air-yard share", format: "num2", heat: true },
    { key: "targets_pg", label: "Tgt/G", title: "Targets per game", format: "num1", heat: true },
    { key: "adot", label: "aDOT", title: "Air yards per target", format: "num1", heat: true },
    ...SHARED_TAIL,
  ],
  RB: [
    ...SHARED_HEAD,
    { key: "designed_rushes_pg", label: "Rush/G", title: "Designed rushes per game (scrambles excluded)", format: "num1", heat: true },
    { key: "backfield_rush_share", label: "Backfield %", title: "Designed rushes / team RB designed rushes", format: "pct", heat: true },
    { key: "targets_pg", label: "Tgt/G", title: "Targets per game", format: "num1", heat: true },
    { key: "rz_carries_pg", label: "RZ car/G", title: "Designed rushes inside the 20 per game", format: "num1", heat: true },
    { key: "gl_carries_pg", label: "GL car/G", title: "Designed rushes inside the 5 per game", format: "num1", heat: true },
    ...SHARED_TAIL,
  ],
  TE: [
    ...SHARED_HEAD,
    { key: "target_share", label: "Tgt %", title: "Targets / team targets over the window", format: "pct", heat: true },
    { key: "targets_pg", label: "Tgt/G", title: "Targets per game", format: "num1", heat: true },
    { key: "rz_targets_pg", label: "RZ tgt/G", title: "Targets inside the 20 per game", format: "num1", heat: true },
    { key: "wopr", label: "WOPR", title: "1.5 × target share + 0.7 × air-yard share", format: "num2", heat: true },
    ...SHARED_TAIL,
  ],
};

export const SORT_KEYS = ["name", "games", "xfp_pg", "ppr_pg", "fpoe_pg", "momentum", "snap_share", "target_share",
  "air_yards_share", "wopr", "targets_pg", "adot", "designed_rushes_pg", "backfield_rush_share", "rz_carries_pg",
  "gl_carries_pg", "rz_targets_pg"] as const;

/** Sort value for a leaderboard column; null sorts last in both directions (lib/sort.ts). */
export function sortValue(p: ResearchPlayer, key: string): number | string | null {
  if (key === "name") return p.name;
  if (key === "games") return p.window.games;
  if (key === "momentum") return p.momentum.xfp_delta;
  const v = (p.window as Record<string, unknown>)[key];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

export type Axis = { key: MetricKey; label: string; format: Format; short: string };
export type WorkloadMap = { title: string; question: string; x: Axis; y: Axis; tooltip: Axis[]; note: string };

const XFP: Axis = { key: "xfp_pg", label: "xFP per game", short: "xFP/G", format: "pts" };
const PPR: Axis = { key: "ppr_pg", label: "PPR per game", short: "PPR/G", format: "pts" };
const FPOE: Axis = { key: "fpoe_pg", label: "FPOE per game", short: "FPOE/G", format: "signed" };
const SNAP: Axis = { key: "snap_share", label: "Snap share", short: "Snap %", format: "pct" };
const TGT_SHARE: Axis = { key: "target_share", label: "Target share", short: "Tgt %", format: "pct" };
const TGT_PG: Axis = { key: "targets_pg", label: "Targets per game", short: "Tgt/G", format: "num1" };

export const WORKLOAD_MAPS: Record<ResearchPosition, WorkloadMap> = {
  WR: {
    title: "Target opportunity map",
    question: "Who earns the passing-game volume, and how much of it is downfield?",
    x: TGT_SHARE,
    y: { key: "air_yards_share", label: "Air-yard share", short: "Air %", format: "pct" },
    tooltip: [{ key: "wopr", label: "WOPR", short: "WOPR", format: "num2" }, TGT_PG,
      { key: "air_yards_pg", label: "Air yards/G", short: "Air yds/G", format: "num1" },
      { key: "adot", label: "aDOT", short: "aDOT", format: "num1" }, PPR, FPOE, SNAP],
    note: "Right = more of the team's targets (volume). Up = more of its air yards (depth). A receiver high and to the left is a downfield role on modest volume; low and to the right is a short-area, reception-volume role.",
  },
  RB: {
    title: "Workhorse and receiving map",
    question: "Who owns the backfield's carries, and who catches passes?",
    x: { key: "backfield_rush_share", label: "Backfield designed-rush share", short: "Backfield %", format: "pct" },
    y: TGT_PG,
    tooltip: [{ key: "designed_rushes_pg", label: "Designed rushes/G", short: "Rush/G", format: "num1" },
      { key: "rz_carries_pg", label: "Red-zone carries/G", short: "RZ car/G", format: "num1" },
      { key: "gl_carries_pg", label: "Goal-line carries/G", short: "GL car/G", format: "num1" }, SNAP,
      { key: "team_rush_share", label: "Team rush share", short: "Team rush %", format: "pct" }, PPR, FPOE],
    note: "Right = early-down rushing role, up = receiving role; up and right is a workhorse, down and left a low-volume backup. Backfield share counts only designed runs by running backs, so a back next to a running quarterback is not penalised for the quarterback's carries; team rush share (in the tooltip) includes them.",
  },
  TE: {
    title: "Participation vs actual opportunity",
    question: "Is the tight end earning targets, or only playing snaps?",
    x: SNAP,
    y: TGT_SHARE,
    tooltip: [TGT_PG, { key: "rz_targets_pg", label: "Red-zone targets/G", short: "RZ tgt/G", format: "num1" },
      { key: "wopr", label: "WOPR", short: "WOPR", format: "num2" }, PPR, FPOE],
    note: "Snap share is offensive snaps, not routes run: a blocking tight end can play most snaps and run few routes. A tight end far right but low is on the field without earning targets.",
  },
};

export const OPPORTUNITY_AXES = { x: XFP, fpoe: FPOE, ppr: PPR };

/** Marker colour token by production group (descriptive, never good/bad). */
export function groupColor(production: string | undefined): string {
  switch (production) {
    case "below": return "var(--chart-1)";
    case "above": return "var(--chart-2)";
    case "inline": return "var(--chart-6)";
    default: return "var(--ink-faint)";
  }
}

export const GROUP_LEGEND = [
  { production: "below", label: "Production below xFP" },
  { production: "inline", label: "In line with xFP" },
  { production: "above", label: "Production above xFP" },
  { production: undefined, label: "Fewer than 3 games (not grouped)" },
] as const;

/** A season with no stored usage at all answers "empty" with no latest week: there is no data to filter, so the page
 * says the season has no data rather than asking to lower the filters. */
export function noSeasonData(r: Pick<OpportunityResponse, "status" | "data">): boolean {
  return r.status === "empty" && (r.data?.latest_week ?? null) === null;
}

/** The volume a filter refers to, in words, for the pool line. */
export function poolSentence(r: Pick<OpportunityResponse, "filters" | "pool" | "windows">): string {
  const f = r.filters;
  const games = `${f.min_games}+ qualifying game${f.min_games === 1 ? "" : "s"}`;
  const vol = f.min_volume > 0 ? ` and ${f.min_volume}+ ${f.volume_label}` : "";
  const team = f.team ? ` on ${f.team}` : "";
  return `${r.pool.shown} of ${r.pool.at_position} ${f.position}s with ${games}${vol}${team} (${r.windows[f.window].toLowerCase()})`;
}

// --------------------------------------------------------------- actions --

export type League = { availability_status: string | null; on_team_id: number | null; my_team_id: number | null; is_mine: boolean } | null;

/** Trade analyzer prefilled with the player (sending the player if yours, receiving otherwise). */
export function tradeHref(league: League, espnId: number | null): string | null {
  if (!league || espnId === null || league.my_team_id === null) return null;
  if (league.is_mine) return `/trades?ta=${league.my_team_id}&a=${espnId}`;
  if (league.availability_status === "ONTEAM" && league.on_team_id !== null) {
    return `/trades?ta=${league.my_team_id}&tb=${league.on_team_id}&b=${espnId}`;
  }
  return null;
}

/** Waiver list for the position when the player is unrostered in the synced league. */
export function waiverHref(league: League, position: ResearchPosition): string | null {
  if (!league) return null;
  return league.availability_status === "FREEAGENT" || league.availability_status === "WAIVERS"
    ? `/waivers?pos=${position}&view=ALL` : null;
}

// ---------------------------------------------------------------- charts --

type Scorer = { value: (p: ResearchPlayer) => number | null | undefined; n: number; dir: "desc" | "asc"; where?: (p: ResearchPlayer) => boolean };

/** Ids labeled by default: the top `n` of each scorer (the chart's extremes), never hardcoded names. */
export function keyIds(players: readonly ResearchPlayer[], scorers: readonly Scorer[]): Set<string> {
  const out = new Set<string>();
  for (const s of scorers) {
    const ranked = players
      .filter((p) => (s.where ? s.where(p) : true))
      .map((p) => ({ id: p.gsis_id, v: s.value(p) }))
      .filter((x): x is { id: string; v: number } => typeof x.v === "number" && Number.isFinite(x.v))
      .sort((a, b) => (s.dir === "desc" ? b.v - a.v : a.v - b.v));
    ranked.slice(0, s.n).forEach((x) => out.add(x.id));
  }
  return out;
}

/** Tooltip rows for a player: [label, formatted value or "—"]. */
export function metricRows(w: WindowSummary, axes: readonly Axis[]): [string, string][] {
  return axes.map((a) => [a.short, formatMetric(metricValue(w, a.key), a.format) ?? "—"]);
}

/** A window metric as a number, or null. */
export function metricValue(w: WindowSummary, key: MetricKey): number | null {
  const v = (w as Record<string, unknown>)[key];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** "Weeks 1–3, 5" style list for a set of game weeks. */
export function weekList(weeks: readonly number[]): string {
  if (weeks.length === 0) return "none";
  const sorted = [...weeks].sort((a, b) => a - b);
  const runs: string[] = [];
  let start = sorted[0];
  let prev = sorted[0];
  for (const w of [...sorted.slice(1), Infinity]) {
    if (w === prev + 1) {
      prev = w;
      continue;
    }
    runs.push(start === prev ? String(start) : prev === start + 1 ? `${start}, ${prev}` : `${start}–${prev}`);
    start = w;
    prev = w;
  }
  return `${sorted.length === 1 ? "Week" : "Weeks"} ${runs.join(", ")}`;
}

/** "12 below the volume filter and 3 with too few games are hidden." ("" when nothing is hidden). */
export function hiddenSentence(excluded: Record<string, number>): string {
  const parts: string[] = [];
  if (excluded.min_volume) parts.push(`${excluded.min_volume} below the volume filter`);
  if (excluded.min_games) parts.push(`${excluded.min_games} with too few games`);
  if (excluded.team) parts.push(`${excluded.team} on other teams`);
  if (excluded.no_qualifying_games) parts.push(`${excluded.no_qualifying_games} with no game that has xFP`);
  if (excluded.limit) parts.push(`${excluded.limit} past the list's limit (the highest xFP/G are listed; raise a filter to narrow it)`);
  if (parts.length === 0) return "";
  const list = parts.length === 1 ? parts[0] : `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
  const total = Object.values(excluded).reduce((a, b) => a + b, 0);
  return `${list[0].toUpperCase()}${list.slice(1)} ${total === 1 ? "is" : "are"} hidden.`;
}
