// Pure logic for the Player Comparison Lab (/compare): the response types of GET /api/compare, URL parsing
// and building, unit formatting, the display rows and the chart series. No React and no `@/` imports, so it
// runs under the unit-test runner (same rule as lib/player-usage.ts).
//
// Nothing here estimates a number. It reads, aligns and formats what the backend stored. A missing value is
// always `null` / "—", never 0; a week with no game is a gap in a series, never a plotted zero.

// ------------------------------------------------------------------------------------------ types --

export type CompareSource = "espn" | "independent";
export type Side = "a" | "b";
export type Edge = "a" | "b" | "even" | "n/a";
export type RowUnit = "pts" | "pts_per_game" | "per_game" | "yards_per_game" | "share" | "games" | "trend" | "rating";
export type RowGroup = "projection" | "usage" | "rating";
/** Where an "Ours" next-week number came from (null when the source is ESPN). */
export type OursOrigin = "ours" | "ours_zero" | "espn" | "bye";

export type Scoring = {
  scoring_type: string | null;
  reception_points: number | null;
  settings_synced: boolean;
  label: string;
  note: string | null;
};

export type ComparePlayer = {
  espn_player_id: number;
  full_name: string | null;
  position: string | null;
  pro_team: string | null;
  fantasy_team: string | null;
  availability_status: string | null;
  percent_owned: number | null;
  gsis_id: string | null;
  match_method: string | null;
  match_confidence: string | null;
};

export type NextWeekProjection = {
  week: number;
  on_bye: boolean | null;
  opponent: string | null;
  is_home: boolean | null;
  espn: number | null;
  ours: number | null;
  ours_origin: OursOrigin | null;
  ours_horizon: number | null;
};

export type RosProjection = {
  espn: number | null;
  espn_weeks_available: number | null;
  espn_weeks_expected: number | null;
  ours: number | null;
  ours_available: boolean | null;
  ours_reason: string | null;
  ours_weeks_available: number | null;
  ours_weeks_expected: number | null;
  ours_experimental: boolean;
};

export type ProjectionWeek = {
  week: number;
  opponent: string | null;
  is_home: boolean | null;
  on_bye: boolean | null;
  phase: string | null;
  espn: number | null;
  ours: number | null;
  ours_origin: OursOrigin | null;
};

export type CardProjection = {
  next_week: NextWeekProjection | null;
  ros: RosProjection | null;
  bye_weeks: number[];
  bye_weeks_remaining: number | null;
  games_remaining: number | null;
  weeks: ProjectionWeek[];
  projection_synced_at: string | null;
};

export type UsageMetric = { value: number | null; applicable: boolean; why: string | null };
export type Rating = { value: number; scale: 100 | 10; legacy: boolean };

export type CardUsage = {
  games: number;
  weeks: number[];
  metrics: Record<string, UsageMetric>;
  trend: { label: string | null; score: number | null };
  ratings: {
    opportunity: Rating | null;
    production: Rating | null;
    basis: string | null;
    version: string | null;
    as_of_week: number | null;
  };
  stability: { cv: number | null; games: number; metric: "xfp" | "opportunities" | null };
};

export type WeeklyEntry = {
  week: number;
  opponent: string | null;
  league_points: number | null;
  ppr: number | null;
  xfp: number | null;
  snap_share: number | null;
  carries: number | null;
  targets: number | null;
};

export type NflReport = {
  week: number;
  report_status: string | null;
  report_primary_injury: string | null;
  practice_status: string | null;
};

export type Availability = {
  espn_status: string | null;
  has_designation: boolean;
  nfl_report: NflReport | null;
  report_is_current: boolean | null;
  label: string;
  note: string;
};

export type RoleEvidence = {
  metric: string;
  label: string;
  baseline_avg: number | null;
  recent_avg: number | null;
  delta: number | null;
  unit: "pct" | "per_game";
  baseline_weeks: number[];
  recent_weeks: number[];
};

export type Role = {
  depth_chart: { pos_abb: string | null; pos_rank: number | null; dt: string | null; team: string | null } | null;
  role_trend: string | null;
  opportunity_tier: string | null;
  evidence: RoleEvidence[];
};

export type CompareCard = {
  player: ComparePlayer;
  projection: CardProjection | null;
  usage: CardUsage;
  weekly: WeeklyEntry[];
  availability: Availability;
  role: Role;
  recent_games: WeeklyEntry[];
};

export type CompareRow = {
  key: string;
  label: string;
  group: RowGroup;
  unit: RowUnit;
  a: number | null;
  b: number | null;
  edge: Edge;
  note: string | null;
};

export type CompareFinding = {
  key: string;
  title: string;
  leader: "a" | "b" | "even" | null;
  text: string;
  basis: string;
};

export type CompareResponse = {
  as_of_week: number;
  completed_through_week: number;
  final_week: number;
  last_regular_season_week: number | null;
  source: CompareSource;
  identical: boolean;
  same_position: boolean;
  scoring: Scoring;
  players: [CompareCard, CompareCard];
  rows: CompareRow[];
  summary: CompareFinding[];
  limitations: string[];
  disclaimer: string;
  freshness: { espn_synced_at: string | null; nfl_synced_at: string | null };
};

// Short aliases used by the page components.
export type Card = CompareCard;
export type Row = CompareRow;
export type Finding = CompareFinding;

// ------------------------------------------------------------------------------------- URL state --

export type IdLike = number | string | null | undefined;
export type SearchParamsLike = Record<string, string | string[] | undefined>;
export type ParamError = { side: Side; value: string; message: string };
export type ParsedCompareParams = { a: number | null; b: number | null; errors: ParamError[] };

const ID_RE = /^-?\d+$/;

/** A clean integer id from a string or number, or null when it is missing or not an id. */
export function cleanId(raw: IdLike): number | null {
  if (raw === null || raw === undefined) return null;
  const text = typeof raw === "number" ? String(raw) : raw.trim();
  if (!ID_RE.test(text)) return null;
  const n = Number(text);
  if (!Number.isSafeInteger(n)) return null;
  return n === 0 ? 0 : n; // "-0" is 0
}

function shown(value: string): string {
  return value.length > 24 ? `${value.slice(0, 23)}…` : value;
}

/**
 * `a` and `b` from the page's search params. A missing or blank side is simply unset; text that is not a whole
 * number (D/ST ids are negative, so a leading minus is fine) is rejected with a message naming the side.
 */
export function parseCompareParams(sp: SearchParamsLike | null | undefined): ParsedCompareParams {
  const errors: ParamError[] = [];
  const side = (key: Side): number | null => {
    const raw = sp?.[key];
    if (raw === undefined) return null;
    if (Array.isArray(raw)) {
      errors.push({ side: key, value: shown(raw.join(",")), message: `Give one player id for ${key === "a" ? "A" : "B"}, not several.` });
      return null;
    }
    const text = raw.trim();
    if (text === "") return null;
    const id = cleanId(text);
    if (id === null) {
      errors.push({
        side: key, value: shown(text),
        message: `“${shown(text)}” is not a player id for ${key === "a" ? "A" : "B"}. Ids are whole numbers (team defenses are negative).`,
      });
    }
    return id;
  };
  const a = side("a");
  const b = side("b");
  return { a, b, errors };
}

/** `/compare?a=..&b=..`. A missing or invalid side is left out, so one player alone is a valid link. */
export function compareHref(a?: IdLike, b?: IdLike): string {
  const parts: string[] = [];
  const ia = cleanId(a);
  const ib = cleanId(b);
  if (ia !== null) parts.push(`a=${ia}`);
  if (ib !== null) parts.push(`b=${ib}`);
  return parts.length ? `/compare?${parts.join("&")}` : "/compare";
}

/** The same two players with the sides exchanged. */
export function swapHref(a?: IdLike, b?: IdLike): string {
  return compareHref(b, a);
}

/** Which side the backend's 404 names: "Unknown ESPN player id 123 (b)". */
export function unknownPlayerSide(detail: string | null | undefined): { side: Side; id: number | null } | null {
  const m = /\((a|b)\)\s*$/.exec((detail ?? "").trim());
  if (!m) return null;
  const idMatch = /player id\s+(-?\d+)/i.exec(detail ?? "");
  return { side: m[1] as Side, id: idMatch ? Number(idMatch[1]) : null };
}

/**
 * "Projections: ESPN" or "Projections: ESPN + Ours"; "Projections: Ours" for a league whose provider publishes no
 * projections (`espnProjections` false, Sleeper).
 */
export function projectionSourceLine(source: CompareSource | "ours" | "espn" | null | undefined, espnProjections = true): string {
  if (!espnProjections) return "Projections: Ours";
  return source === "independent" || source === "ours"
    ? "Projections: ESPN + Ours"
    : "Projections: ESPN";
}

// ----------------------------------------------------------------------------------- formatting --

export const DASH = "—";

const finite = (v: number | null | undefined): v is number => typeof v === "number" && Number.isFinite(v);

/** What a rating cell needs to print its scale. Without it a rating prints as a bare number: never guessed. */
export type RatingScale = { scale?: number | null; legacy?: boolean };

/**
 * One value in its unit. `null` is "—" (never 0). A rating prints with its own scale: "87.4/100", or the
 * legacy "8.7/10 (legacy)"; a 0–10 value is never printed as /100.
 */
export function formatRowValue(unit: RowUnit, v: number | null | undefined, rating?: RatingScale): string {
  if (!finite(v)) return DASH;
  switch (unit) {
    case "share":
      return `${(v * 100).toFixed(1)}%`;
    case "games":
      return String(Math.round(v));
    case "trend": {
      const s = Math.abs(v).toFixed(2);
      if (Number(s) === 0) return "0.00";
      return `${v > 0 ? "+" : "−"}${s}`;
    }
    case "rating": {
      const body = v.toFixed(1);
      const scale = rating?.scale;
      if (scale === 10) return `${body}/10 (legacy)`;
      if (scale === 100) return rating?.legacy ? `${body}/100 (legacy)` : `${body}/100`;
      return body;
    }
    default: // pts, pts_per_game, per_game, yards_per_game
      return v.toFixed(1);
  }
}

/** The league's scoring name, with a fallback for a response that carries none. */
export function scoringLabel(scoring: Pick<Scoring, "label"> | null | undefined): string {
  const l = scoring?.label?.trim();
  return l ? l : "League scoring";
}

/** What a row's unit means: league-scoring rows name the league's scoring, nflverse PPR rows say so. */
export function unitLabel(unit: RowUnit, scoring: Pick<Scoring, "label"> | null | undefined, key?: string): string {
  if (key === "ppr_pg") return "PPR (nflverse)";
  if (key === "xfp_pg") return "Expected PPR (nflverse)";
  const league = scoringLabel(scoring);
  if (key === "league_pts_pg") return `${league}, per game`;
  switch (unit) {
    case "pts": return `Points, ${league}`;
    case "pts_per_game": return `Points per game, ${league}`;
    case "per_game": return "Per game";
    case "yards_per_game": return "Yards per game";
    case "share": return "Share of team";
    case "games": return "Games";
    case "trend": return "−1 to +1";
    case "rating": return "Percentile within position (scale shown on each value)";
  }
}

// ------------------------------------------------------------------------------------------ edges --

/**
 * Short visible marker for the column between the two players: an arrow pointing at the side with the edge,
 * "=" for even, "–" for not comparable. Always paired with `edgeText` for assistive tech.
 */
export function edgeMark(edge: Edge): string {
  return edge === "a" ? "◀" : edge === "b" ? "▶" : edge === "even" ? "=" : "–";
}

/** Accessible text for an edge: "Edge: Name", "Even" or "Not comparable". */
export function edgeText(edge: Edge, nameA: string, nameB: string): string {
  if (edge === "a") return `Edge: ${nameA}`;
  if (edge === "b") return `Edge: ${nameB}`;
  return edge === "even" ? "Even" : "Not comparable";
}

export function playerName(card: Pick<CompareCard, "player"> | null | undefined): string {
  const p = card?.player;
  return p?.full_name?.trim() || (p ? `Player #${p.espn_player_id}` : "Unknown player");
}

export function sideCard(resp: CompareResponse, side: Side): CompareCard {
  return resp.players[side === "a" ? 0 : 1];
}

/** The name of a finding's leader, or why there is none. */
export function leaderLabel(leader: CompareFinding["leader"], nameA: string, nameB: string): string {
  if (leader === "a") return nameA;
  if (leader === "b") return nameB;
  return leader === "even" ? "Even" : "No call";
}

// ------------------------------------------------------------------------------ display rows --

export type CellTag = "espn_fallback" | "experimental" | "zero" | "bye";

export type Cell = {
  text: string;
  missing: boolean;
  /** Why the value is absent or not applicable (shown beside the dash). */
  why: string | null;
  tag?: CellTag;
};

export type DisplayRow = {
  key: string;
  label: string;
  unit: string;
  edge: Edge;
  note: string | null;
  experimental: boolean;
  a: Cell;
  b: Cell;
};

/** The rows that belong to the projection section; every other row is a usage or rating row. */
export const PROJECTION_KEYS = ["next_week_espn", "next_week_ours", "ros_espn", "ros_ours", "games_remaining"];

export function isProjectionRow(row: Pick<CompareRow, "key" | "group">): boolean {
  return PROJECTION_KEYS.includes(row.key) || row.group === "projection";
}

function ratingOf(card: CompareCard, key: string): Rating | null {
  if (key === "opportunity_rating") return card.usage?.ratings?.opportunity ?? null;
  if (key === "production_rating") return card.usage?.ratings?.production ?? null;
  return null;
}

/** The reason a value is missing for one side of one row, from the card the value would have come from. */
export function missingWhy(card: CompareCard, row: Pick<CompareRow, "key" | "note">): string {
  const metric = card.usage?.metrics?.[row.key];
  if (metric) {
    if (metric.why) return metric.why;
    if (!metric.applicable) return `Not applicable to ${card.player.position ?? "this position"}.`;
    return "No usage record.";
  }
  const proj = card.projection;
  const nw = proj?.next_week ?? null;
  switch (row.key) {
    case "next_week_espn":
      if (!proj) return "The league's projection window is unknown.";
      if (nw?.on_bye) return `On bye in week ${nw.week}.`;
      return "No ESPN projection stored for this week.";
    case "next_week_ours":
      if (!proj) return "The league's projection window is unknown.";
      if (nw?.on_bye || nw?.ours_origin === "bye") return `On bye in week ${nw?.week ?? ""}`.trim() + ".";
      if (nw?.ours_origin === "espn") return "No independent estimate for this player; ESPN's number is the only one.";
      return "No Fantasy Intel projection for this week.";
    case "ros_espn": {
      const r = proj?.ros;
      if (!r) return "The league's projection window is unknown.";
      if (finite(r.espn_weeks_available) && finite(r.espn_weeks_expected) && r.espn_weeks_available < r.espn_weeks_expected) {
        return `ESPN projects ${r.espn_weeks_available} of ${r.espn_weeks_expected} remaining weeks, so no total is shown.`;
      }
      return "No complete ESPN rest-of-season total.";
    }
    case "ros_ours": {
      const r = proj?.ros;
      // The backend's reason can be a paragraph of model governance; it is listed once under the limits instead.
      if (r && finite(r.ours_weeks_available) && finite(r.ours_weeks_expected) && r.ours_weeks_available < r.ours_weeks_expected) {
        return `Our model covers ${r.ours_weeks_available} of ${r.ours_weeks_expected} remaining weeks, so no total is shown.`;
      }
      return r?.ours_reason ? "No complete total from our model; the reason is under the limits above." : "No complete rest-of-season total from our model.";
    }
    case "games_remaining":
      return "A remaining week's schedule is unknown, so the count is not shown.";
    case "opportunity_rating":
    case "production_rating":
      return "No rating for this player.";
    case "trend_score":
      return "Not enough games for a usage trend.";
  }
  return row.note ?? "No data.";
}

/** One side's cell for a row: the formatted value, or a dash with the reason. Never a 0 for a missing number. */
export function rowCell(resp: CompareResponse, row: CompareRow, side: Side): Cell {
  const card = sideCard(resp, side);
  const v = row[side];
  const nw = card.projection?.next_week ?? null;

  if (row.key === "next_week_espn" && nw?.on_bye) {
    return { text: DASH, missing: true, why: `On bye in week ${nw.week}.`, tag: "bye" };
  }
  if (row.key === "next_week_ours") {
    if (nw?.on_bye || nw?.ours_origin === "bye") {
      return { text: DASH, missing: true, why: `On bye in week ${nw?.week ?? ""}`.trim() + ".", tag: "bye" };
    }
    if (nw?.ours_origin === "espn") {
      return { text: DASH, missing: true, why: missingWhy(card, row), tag: "espn_fallback" };
    }
    if (nw?.ours_origin === "ours_zero" && finite(v)) {
      return { text: formatRowValue("pts", v), missing: false, why: "Our model projects no points (unavailable or inactive).", tag: "zero" };
    }
  }
  if (!finite(v)) return { text: DASH, missing: true, why: missingWhy(card, row) };
  const rating = ratingOf(card, row.key);
  return { text: formatRowValue(row.unit, v, rating ? { scale: rating.scale, legacy: rating.legacy } : undefined), missing: false, why: null };
}

function displayRow(resp: CompareResponse, row: CompareRow): DisplayRow {
  return {
    key: row.key,
    label: row.label,
    unit: unitLabel(row.unit, resp.scoring, row.key),
    edge: row.edge,
    note: row.note,
    experimental: row.key === "ros_ours",
    a: rowCell(resp, row, "a"),
    b: rowCell(resp, row, "b"),
  };
}

/** The rows that carry ESPN's projection: left out for a league without ESPN projections (they would be all dashes). */
export const ESPN_PROJECTION_KEYS = ["next_week_espn", "ros_espn"];

const keepRow = (espnProjections: boolean) => (r: Pick<CompareRow, "key">) => espnProjections || !ESPN_PROJECTION_KEYS.includes(r.key);

/** Rows for the projection section, in the backend's order (without the ESPN rows when `espnProjections` is false). */
export function projectionRows(resp: CompareResponse, espnProjections = true): DisplayRow[] {
  return resp.rows.filter(isProjectionRow).filter(keepRow(espnProjections)).map((r) => displayRow(resp, r));
}

/** Rows for the usage section: usage metrics, trend and ratings. */
export function usageRows(resp: CompareResponse): DisplayRow[] {
  return resp.rows.filter((r) => !isProjectionRow(r)).map((r) => displayRow(resp, r));
}

/** Every row, for the compact advantages table (without the ESPN rows when `espnProjections` is false). */
export function allRows(resp: CompareResponse, espnProjections = true): DisplayRow[] {
  return resp.rows.filter(keepRow(espnProjections)).map((r) => displayRow(resp, r));
}

// -------------------------------------------------------------------- projection context cells --

function oppText(w: Pick<ProjectionWeek, "opponent" | "is_home" | "on_bye">): string {
  if (w.on_bye) return "BYE";
  if (!w.opponent) return "TBD";
  return `${w.is_home === false ? "@" : "vs "}${w.opponent}`;
}

/** "Wk 7 vs BUF", "Wk 7 BYE", or null when the projection window is unknown. */
export function nextOpponentText(card: CompareCard): string | null {
  const nw = card.projection?.next_week;
  if (!nw) return null;
  return `Wk ${nw.week} ${oppText(nw)}`;
}

/** The next few opponents as "7 vs BUF · 8 @KC · 9 BYE". */
export function upcomingText(card: CompareCard, count = 4): string | null {
  const weeks = [...(card.projection?.weeks ?? [])].sort((x, y) => x.week - y.week).slice(0, count);
  if (weeks.length === 0) return null;
  return weeks.map((w) => `${w.week} ${oppText(w)}`).join(" · ");
}

/** Bye week(s) as "Week 9" / "Weeks 9, 12", or null when none is listed. */
export function byeText(card: CompareCard): string | null {
  const byes = card.projection?.bye_weeks ?? [];
  if (byes.length === 0) return null;
  return `${byes.length === 1 ? "Week" : "Weeks"} ${byes.join(", ")}`;
}

// ------------------------------------------------------------------------------- availability --

export type ReportCurrency = "current" | "stale" | "none" | "unknown";

export type AvailabilityView = {
  label: string;
  note: string;
  report: string | null;
  currency: ReportCurrency;
  currencyText: string;
};

/** The injury designation, its NFL report line and whether that report is for this week. */
export function availabilityView(card: CompareCard, asOfWeek: number): AvailabilityView {
  const av = card.availability;
  const rep = av.nfl_report;
  let currency: ReportCurrency = "none";
  let currencyText = "No NFL report on file";
  if (rep) {
    if (av.report_is_current === true) {
      currency = "current";
      currencyText = `Current: week ${rep.week} report`;
    } else if (av.report_is_current === false) {
      currency = "stale";
      currencyText = `Stale: week ${rep.week} report, this week is ${asOfWeek}`;
    } else {
      currency = "unknown";
      currencyText = `Week ${rep.week} report; currency unknown`;
    }
  }
  const parts = rep ? [rep.report_status, rep.report_primary_injury, rep.practice_status ? `practice: ${rep.practice_status}` : null] : [];
  return { label: av.label, note: av.note, report: parts.filter(Boolean).join(" · ") || null, currency, currencyText };
}

const METRIC_NAMES: Record<string, string> = {
  offense_pct: "Snap share",
  snap_share: "Snap share",
  target_share: "Target share",
  air_yards_share: "Air-yard share",
  team_rush_share: "Team rush share",
  backfield_rush_share: "Backfield rush share",
  targets: "Targets",
  carries: "Carries",
  xfp: "xFP",
  ppr: "PPR points",
};

/** A role-evidence metric key as a name for people ("offense_pct" is "Snap share"). */
export function metricName(key: string): string {
  return METRIC_NAMES[key] ?? key.replaceAll("_", " ").replace(/^\w/, (c) => c.toUpperCase());
}

/** One role-evidence line: "Target share 22.0% → 31.0% (+9.0 pts)". */
export function formatEvidence(e: RoleEvidence): string {
  const name = metricName(e.metric);
  const delta = e.delta ?? (finite(e.recent_avg) && finite(e.baseline_avg) ? e.recent_avg - e.baseline_avg : null);
  if (e.unit === "pct") {
    const from = formatRowValue("share", e.baseline_avg);
    const to = formatRowValue("share", e.recent_avg);
    const d = finite(delta) ? ` (${delta >= 0 ? "+" : "−"}${Math.abs(delta * 100).toFixed(1)} pts)` : "";
    return `${name} ${from} → ${to}${d}`;
  }
  const from = formatRowValue("per_game", e.baseline_avg);
  const to = formatRowValue("per_game", e.recent_avg);
  const d = finite(delta) ? ` (${delta >= 0 ? "+" : "−"}${Math.abs(delta).toFixed(1)})` : "";
  return `${name} ${from} → ${to} per game${d}`;
}

// ----------------------------------------------------------------------------------- chart series --

/** Structurally the same as the charts' LineSeries, without importing it. */
export type ChartSeries = { id: string; label: string; points: (number | null)[] };

const MAX_WEEKS = 30;

/** Weeks 1..n, for n completed weeks (empty for anything that is not a positive whole number). */
export function weekAxis(completedThrough: number | null | undefined): number[] {
  if (!finite(completedThrough) || completedThrough < 1) return [];
  const n = Math.min(MAX_WEEKS, Math.floor(completedThrough));
  return Array.from({ length: n }, (_, i) => i + 1);
}

/** A card's weekly league points aligned on `weeks`; a bye or a week with no ESPN score is a gap (null), never 0. */
export function weeklyLeaguePoints(card: CompareCard, weeks: readonly number[]): (number | null)[] {
  const byWeek = new Map<number, number | null>();
  for (const w of card.weekly ?? []) byWeek.set(w.week, finite(w.league_points) ? w.league_points : null);
  return weeks.map((wk) => byWeek.get(wk) ?? null);
}

export type WeeklyChart = { weeks: number[]; xLabels: string[]; series: ChartSeries[] };

/** Weekly league points for both players on weeks 1..completed_through_week, or null when neither has a point. */
export function weeklyPointsChart(resp: CompareResponse): WeeklyChart | null {
  const weeks = weekAxis(resp.completed_through_week);
  if (weeks.length === 0) return null;
  const [a, b] = resp.players;
  const series: ChartSeries[] = [
    { id: "a", label: playerName(a), points: weeklyLeaguePoints(a, weeks) },
    { id: "b", label: playerName(b), points: weeklyLeaguePoints(b, weeks) },
  ];
  if (!series.some((s) => s.points.some((p) => p !== null))) return null;
  return { weeks, xLabels: weeks.map(String), series };
}

/** The remaining weeks of both players: the union of their projection weeks, ascending. */
export function remainingWeeks(resp: CompareResponse): number[] {
  const set = new Set<number>();
  for (const card of resp.players) for (const w of card.projection?.weeks ?? []) set.add(w.week);
  return [...set].sort((x, y) => x - y);
}

function weekMap(card: CompareCard): Map<number, ProjectionWeek> {
  return new Map((card.projection?.weeks ?? []).map((w) => [w.week, w]));
}

/** ESPN's projection per week; a bye, a week outside the player's schedule or a missing projection is a gap. */
export function espnProjectionSeries(card: CompareCard, weeks: readonly number[]): (number | null)[] {
  const m = weekMap(card);
  return weeks.map((wk) => {
    const w = m.get(wk);
    if (!w || w.on_bye || !finite(w.espn)) return null;
    return w.espn;
  });
}

/**
 * Our model's number per week, only where it is ours: origin `ours`, or `ours_zero` away from a bye (the model's
 * own 0 for an unavailable player). ESPN fallbacks, byes and missing weeks are gaps.
 */
export function oursProjectionSeries(card: CompareCard, weeks: readonly number[]): (number | null)[] {
  const m = weekMap(card);
  return weeks.map((wk) => {
    const w = m.get(wk);
    if (!w || w.on_bye || !finite(w.ours)) return null;
    return w.ours_origin === "ours" || w.ours_origin === "ours_zero" ? w.ours : null;
  });
}

export type RemainingChart = {
  weeks: number[];
  xLabels: string[];
  espn: ChartSeries[];
  /** Present only for source=independent and when at least one of our numbers exists. */
  ours: ChartSeries[] | null;
};

/** Remaining-season projections for both players on the union of their remaining weeks, or null with none. */
export function remainingProjectionChart(resp: CompareResponse): RemainingChart | null {
  const weeks = remainingWeeks(resp);
  if (weeks.length === 0) return null;
  const [a, b] = resp.players;
  const espn: ChartSeries[] = [
    { id: "a", label: playerName(a), points: espnProjectionSeries(a, weeks) },
    { id: "b", label: playerName(b), points: espnProjectionSeries(b, weeks) },
  ];
  let ours: ChartSeries[] | null = null;
  if (resp.source === "independent") {
    const o: ChartSeries[] = [
      { id: "a", label: playerName(a), points: oursProjectionSeries(a, weeks) },
      { id: "b", label: playerName(b), points: oursProjectionSeries(b, weeks) },
    ];
    if (o.some((s) => s.points.some((p) => p !== null))) ours = o;
  }
  if (!espn.some((s) => s.points.some((p) => p !== null)) && !ours) return null;
  return { weeks, xLabels: weeks.map(String), espn, ours };
}

export type OpportunityPoint = { id: Side; label: string; x: number; y: number };

/** Opportunity (xFP/g) against production (PPR/g), one point per player, only when both numbers exist. */
export function opportunityPoints(resp: CompareResponse): OpportunityPoint[] {
  const out: OpportunityPoint[] = [];
  (["a", "b"] as const).forEach((side) => {
    const card = sideCard(resp, side);
    const x = card.usage?.metrics?.xfp_pg;
    const y = card.usage?.metrics?.ppr_pg;
    if (x && y && x.applicable && y.applicable && finite(x.value) && finite(y.value)) {
      out.push({ id: side, label: playerName(card), x: x.value, y: y.value });
    }
  });
  return out;
}

// ---------------------------------------------------------------------------------- Ours helpers --

export type OursNextKind = "ours" | "zero" | "bye" | "espn" | "missing";

/** What the Ours next-week number is for a card: ours, our own 0, a bye, ESPN's stand-in (no Ours number) or absent. */
export function nextWeekOurs(card: CompareCard): { kind: OursNextKind; value: number | null } {
  const nw = card.projection?.next_week;
  if (!nw) return { kind: "missing", value: null };
  if (nw.on_bye || nw.ours_origin === "bye") return { kind: "bye", value: null };
  if (nw.ours_origin === "espn") return { kind: "espn", value: null };
  if (nw.ours_origin === "ours_zero") return { kind: "zero", value: finite(nw.ours) ? nw.ours : 0 };
  if (nw.ours_origin === "ours" && finite(nw.ours)) return { kind: "ours", value: nw.ours };
  return { kind: "missing", value: null };
}

// ----------------------------------------------------------------------------------- shape guard --

/** True when the payload has the two cards and the row list the page reads. */
export function isCompareResponse(x: unknown): x is CompareResponse {
  if (!x || typeof x !== "object") return false;
  const r = x as Partial<CompareResponse>;
  return Array.isArray(r.players) && r.players.length === 2 && !!r.players[0]?.player && !!r.players[1]?.player
    && Array.isArray(r.rows) && Array.isArray(r.summary);
}

/** A row's note without its "Not applicable for …" parts: each cell already prints that player's reason. */
export function rowNote(note: string | null | undefined): string | null {
  return note?.split("; ").filter((n) => !n.startsWith("Not applicable for ")).join("; ") || null;
}

// ================================================================================ shortlist (#175) --
// Two to four players for one lineup slot (GET /api/compare/shortlist). The same player cards as the pair view; the
// backend adds the lineup engine's slot eligibility, roster status for the selected team and the best stored
// projection among the players who can fill the slot. The start pick is the viewer's own choice, kept in the URL; this
// module never names one.

export const MAX_SHORTLIST = 4;
export const MIN_SHORTLIST = 2;
/** Every starting slot the lineup engine fills; a league offers the subset it starts (the response's `slots`). */
export const ENGINE_SLOTS = ["QB", "RB", "WR", "TE", "K", "D/ST", "RB/WR", "WR/TE", "FLEX", "OP"] as const;

export type RosterStatus = "this_team" | "other_team" | "free_agent" | "waivers" | "unknown";
export type BestStatus = "leader" | "close" | "only" | "incomplete" | "none";

export type ShortlistSlot = { slot: string; count: number; positions: string[] };

export type ShortlistPlayer = {
  card: CompareCard;
  roster: RosterStatus;
  eligible_slots: string[];
  slot_eligible: boolean | null;
};

export type BestProjected = {
  source: "espn" | "ours";
  /** The source named on the page: "ESPN" or "Fantasy Intel" only when some player has a number from it, else "Unavailable". */
  source_label: string;
  week: number;
  slot: string | null;
  leaders: number[];
  value: number | null;
  gap: number | null;
  considered: number[];
  excluded: { espn_player_id: number; reason: "not_eligible" | "bye" | "no_projection"; text: string }[];
  complete: boolean;
  status: BestStatus;
  text: string;
};

export type ShortlistResponse = {
  as_of_week: number;
  completed_through_week: number;
  final_week: number | null;
  source: CompareSource;
  provider: string;
  scoring: Scoring;
  slots: ShortlistSlot[];
  unsupported_slots: string[];
  slot: string | null;
  team: { team_id: number; name: string; is_owner_default: boolean } | null;
  players: ShortlistPlayer[];
  best_projected: BestProjected;
  limitations: string[];
  disclaimer: string;
  freshness: { espn_synced_at: string | null; nfl_synced_at: string | null };
};

export type ShortlistParams = { ids: number[]; slot: string | null; start: number | null; errors: string[] };

const firstParam = (v: string | string[] | undefined): string | undefined => (Array.isArray(v) ? v[0] : v);

/** The shortlist view is asked for by an `ids` param and no pair (`a`/`b` always open the pair view, as before). */
export function isShortlistView(sp: SearchParamsLike | null | undefined): boolean {
  return !!sp && sp.ids !== undefined && sp.a === undefined && sp.b === undefined;
}

/**
 * `ids`, `slot` and `start` of the shortlist URL. Junk ids are named and skipped, a repeat is dropped, and only the
 * first MAX_SHORTLIST are kept. A slot outside ENGINE_SLOTS is refused; a start pick not on the list is ignored.
 */
export function parseShortlistParams(sp: SearchParamsLike | null | undefined): ShortlistParams {
  const errors: string[] = [];
  const raw = sp?.ids;
  const text = Array.isArray(raw) ? raw.join(",") : raw ?? "";
  const ids: number[] = [];
  for (const part of text.split(",").map((p) => p.trim()).filter(Boolean)) {
    const id = cleanId(part);
    if (id === null) errors.push(`“${shown(part)}” is not a player id. Ids are whole numbers (team defenses are negative).`);
    else if (!ids.includes(id)) ids.push(id);
  }
  if (ids.length > MAX_SHORTLIST) {
    errors.push(`A shortlist holds up to ${MAX_SHORTLIST} players; the first ${MAX_SHORTLIST} are kept.`);
    ids.length = MAX_SHORTLIST;
  }
  const slotRaw = firstParam(sp?.slot)?.trim().toUpperCase() ?? "";
  let slot: string | null = null;
  if (slotRaw !== "") {
    if ((ENGINE_SLOTS as readonly string[]).includes(slotRaw)) slot = slotRaw;
    else errors.push(`“${shown(slotRaw)}” is not a lineup slot. Pick one of the slots above.`);
  }
  const start = cleanId(firstParam(sp?.start));
  return { ids, slot, start: start !== null && ids.includes(start) ? start : null, errors };
}

export type ShortlistState = { ids: readonly number[]; slot?: string | null; start?: number | null; team?: string | number | null };

/** `/compare?ids=1,2,3&slot=FLEX&start=2&team=4`. `ids` is always written (empty too), so the link stays in this view. */
export function shortlistHref({ ids, slot, start, team }: ShortlistState): string {
  const list = [...new Set(ids.map((i) => cleanId(i)).filter((i): i is number => i !== null))].slice(0, MAX_SHORTLIST);
  const parts = [`ids=${list.join(",")}`];
  if (slot) parts.push(`slot=${encodeURIComponent(slot)}`);
  if (start !== null && start !== undefined && list.includes(start)) parts.push(`start=${start}`);
  if (team !== null && team !== undefined && String(team) !== "") parts.push(`team=${encodeURIComponent(String(team))}`);
  return `/compare?${parts.join("&")}`;
}

/** The list with `id` added at the end (unchanged when it is already there or the list is full). */
export function withPlayer(ids: readonly number[], id: number): number[] {
  return ids.includes(id) || ids.length >= MAX_SHORTLIST ? [...ids] : [...ids, id];
}

/** The list without `id`. */
export function withoutPlayer(ids: readonly number[], id: number): number[] {
  return ids.filter((i) => i !== id);
}

/** Every pair of the shortlist, in list order: each opens the existing two-player comparison. */
export function shortlistPairs<T>(items: readonly T[]): [T, T][] {
  const out: [T, T][] = [];
  for (let i = 0; i < items.length; i++) for (let j = i + 1; j < items.length; j++) out.push([items[i], items[j]]);
  return out;
}

/** The pair view for two shortlist players, carrying the shortlist so the page can link back to it. */
export function pairFromShortlistHref(a: number, b: number, state: ShortlistState): string {
  const back = shortlistHref(state).replace(/^\/compare\?/, "");
  return `${compareHref(a, b)}&${back}`;
}

/** Whether a pair-view URL came from a shortlist (it carries `ids`), and the shortlist link back. */
export function shortlistBackHref(sp: SearchParamsLike | null | undefined): string | null {
  if (!sp || sp.ids === undefined) return null;
  const p = parseShortlistParams(sp);
  if (p.ids.length === 0) return null;
  return shortlistHref({ ids: p.ids, slot: p.slot, start: p.start, team: firstParam(sp.team) ?? null });
}

/** The shortlist that holds the pair being viewed (or nothing yet): the way from the pair view to the shortlist. */
export function shortlistFromPair(a: IdLike, b: IdLike): string {
  return shortlistHref({ ids: [cleanId(a), cleanId(b)].filter((i): i is number => i !== null) });
}

/** Unknown-player 404 of the shortlist: "Unknown ESPN player id 999 (player 3)". */
export function unknownShortlistId(detail: string | null | undefined): number | null {
  const m = /player id\s+(-?\d+)\s*\(player \d+\)\s*$/i.exec((detail ?? "").trim());
  return m ? Number(m[1]) : null;
}

/** Roster status in words, for the team the view is for. */
export function rosterText(p: Pick<ShortlistPlayer, "roster" | "card">, teamName: string | null): string {
  switch (p.roster) {
    case "this_team":
      return teamName ? `On ${teamName}` : "On this team";
    case "other_team":
      return p.card.player.fantasy_team ? `On ${p.card.player.fantasy_team}` : "On another team";
    case "free_agent":
      return "Free agent";
    case "waivers":
      return "On waivers";
    default:
      return "Roster status unknown";
  }
}

/** The short roster tag for the grid ("Yours", "FA", ...). */
export function rosterShort(roster: RosterStatus): string {
  return { this_team: "Yours", other_team: "Rostered", free_agent: "FA", waivers: "Waivers", unknown: DASH }[roster];
}

/** Which next-week number the shortlist ranks on (the source the page shows). */
export function shortlistField(r: Pick<ShortlistResponse, "best_projected">): "espn" | "ours" {
  return r.best_projected.source;
}

export type ShortCell = { text: string; kind: "value" | "na" | "missing" | "bye"; why: string | null };

/** The next-week projection cell: a bye is "Bye", a missing number is "—" with why, never 0. */
export function nextWeekCell(card: CompareCard, field: "espn" | "ours"): ShortCell {
  const nw = card.projection?.next_week;
  if (!nw) return { text: DASH, kind: "missing", why: "The league's projection window is unknown." };
  if (nw.on_bye) return { text: "Bye", kind: "bye", why: `On bye in week ${nw.week}.` };
  const v = nw[field];
  if (!finite(v)) {
    const src = field === "ours" ? "Fantasy Intel" : "ESPN";
    return { text: DASH, kind: "missing", why: `No ${src} projection for week ${nw.week}.` };
  }
  return { text: v.toFixed(1), kind: "value", why: null };
}

/** The rest-of-season cell, missing with the reason when not every remaining week is projected. */
export function rosCell(card: CompareCard, field: "espn" | "ours"): ShortCell {
  const ros = card.projection?.ros;
  if (!ros) return { text: DASH, kind: "missing", why: "The league's projection window is unknown." };
  const v = ros[field];
  if (finite(v)) return { text: v.toFixed(1), kind: "value", why: null };
  if (field === "ours") return { text: DASH, kind: "missing", why: ros.ours_reason ?? "No Fantasy Intel rest-of-season total." };
  return {
    text: DASH, kind: "missing",
    why: ros.espn_weeks_expected !== null && ros.espn_weeks_available !== null
      ? `ESPN projects ${ros.espn_weeks_available} of ${ros.espn_weeks_expected} remaining weeks.`
      : "No ESPN rest-of-season total.",
  };
}

/** A usage metric cell: "N/A" with why when it does not apply to the position, "—" with why when missing. */
export function metricCell(card: CompareCard, key: string, unit: RowUnit): ShortCell {
  const m = card.usage.metrics[key];
  if (!m) return { text: DASH, kind: "missing", why: "Not in the usage data." };
  if (!m.applicable) return { text: "N/A", kind: "na", why: m.why ?? `Not a measure for ${card.player.position ?? "this position"}.` };
  if (!finite(m.value)) return { text: DASH, kind: "missing", why: m.why ?? "No data." };
  return { text: formatRowValue(unit, m.value), kind: "value", why: null };
}

/** The injury designation, short ("Questionable") with the full label as why; none reported is said so, not "healthy". */
export function injuryCell(card: CompareCard): ShortCell {
  const av = card.availability;
  if (!av.has_designation) return { text: "None reported", kind: "value", why: av.note };
  const short = av.label.split(" (")[0];
  return { text: short, kind: "value", why: `${av.label}. ${av.note}` };
}

/** "UP", "DOWN", "FLAT" trend labels as words; a missing trend is "—". */
export function trendCell(label: string | null | undefined, what: string): ShortCell {
  if (!label || label === "INSUFFICIENT") return { text: DASH, kind: "missing", why: `${what} is not measurable (too few games).` };
  const words: Record<string, string> = { UP: "Up", DOWN: "Down", FLAT: "Flat" };
  return { text: words[label] ?? label.charAt(0) + label.slice(1).toLowerCase(), kind: "value", why: null };
}

/**
 * A shortlist row's source hint: "ESPN" or "Ours" only when some player has a number from it for that row, else
 * "Unavailable" (D-14: a source with no values is never named). The next-week row follows the backend's
 * `source_label`; the rest-of-season row is read from the ROS totals themselves, since everyone can be on bye or
 * unprojected this week while their remaining-season totals exist.
 */
export function shortlistSourceHint(r: Pick<ShortlistResponse, "best_projected" | "players">, row: "next" | "ros"): string {
  const field = r.best_projected.source;
  const name = field === "ours" ? "Ours" : "ESPN";
  if (row === "next") return r.best_projected.source_label === "Unavailable" ? "Unavailable" : name;
  return r.players.some((p) => finite(p.card.projection?.ros?.[field])) ? name : "Unavailable";
}

/** Whether the player is (one of) the best projected. */
export function isBestProjected(r: Pick<ShortlistResponse, "best_projected">, id: number): boolean {
  return r.best_projected.leaders.includes(id);
}

/**
 * How the viewer's start pick stands against the best projected, from the backend's numbers only: the same player,
 * a gap in projected points, or why there is nothing to compare. Null without a pick.
 */
export function startPickNote(r: ShortlistResponse, start: number | null): string | null {
  if (start === null) return null;
  const p = r.players.find((x) => x.card.player.espn_player_id === start);
  if (!p) return null;
  const name = playerName(p.card);
  const best = r.best_projected;
  const src = best.source_label;
  if (p.slot_eligible === false) return `Your pick, ${name}, cannot start at ${r.slot}.`;
  if (best.leaders.includes(start)) {
    return best.leaders.length > 1
      ? `Your pick, ${name}, is among the players tied for the best ${src} projection.`
      : `Your pick, ${name}, is also the best projected (${src}).`;
  }
  const cell = nextWeekCell(p.card, best.source);
  if (cell.kind === "bye") return `Your pick, ${name}, is on bye in week ${best.week}.`;
  if (cell.kind !== "value" || best.value === null) {
    return `Your pick, ${name}, has no ${best.source_label === "Unavailable" ? "" : `${src} `}projection to compare with.`;
  }
  const own = best.source === "ours" ? p.card.projection?.next_week?.ours : p.card.projection?.next_week?.espn;
  const gap = finite(own) ? best.value - own : null;
  return gap === null ? null : `Your pick, ${name}, is projected ${gap.toFixed(1)} points below the best projected (${src}).`;
}

/** The verdict headline from the best-projected block (who, never "start"). */
export function bestHeadline(r: ShortlistResponse): string {
  const b = r.best_projected;
  const names = b.leaders.map((id) => {
    const p = r.players.find((x) => x.card.player.espn_player_id === id);
    return p ? playerName(p.card) : `Player ${id}`;
  });
  const where = b.slot ? ` at ${b.slot}` : "";
  if (b.status === "none" || names.length === 0) return `No best projected${where} for week ${b.week}`;
  if (names.length > 1) return `${names.join(" and ")} are tied for best projected${where}`;
  if (b.status === "incomplete") return `${names[0]} is best projected${where} of the players with a projection`;
  return `${names[0]} is best projected${where} for week ${b.week}`;
}

/** True when the payload has the player list and best-projected block the shortlist page reads. */
export function isShortlistResponse(x: unknown): x is ShortlistResponse {
  if (!x || typeof x !== "object") return false;
  const r = x as Partial<ShortlistResponse>;
  return Array.isArray(r.players) && r.players.every((p) => !!p?.card?.player) && !!r.best_projected
    && Array.isArray(r.best_projected.leaders) && Array.isArray(r.slots);
}
