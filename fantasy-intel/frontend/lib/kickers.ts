// Types and pure helpers for the kicker projections page (GET /api/projections/kickers, kfp-1.1.0).

export type DistanceKey = "0_39" | "40_49" | "50_59" | "60_plus";
export const DISTANCES: { key: DistanceKey; label: string }[] = [
  { key: "0_39", label: "0–39" },
  { key: "40_49", label: "40–49" },
  { key: "50_59", label: "50–59" },
  { key: "60_plus", label: "60+" },
];

export type StatLine = {
  fg_att: number;
  fg_made: number;
  pat_att: number;
  pat_made: number;
  fg_att_by_distance: Record<DistanceKey, number>;
  fg_made_by_distance: Record<DistanceKey, number>;
  p_make_by_distance: Record<DistanceKey, number>;
  p_pat: number;
};

export type Weather = {
  status: "ok" | "stale" | "unavailable" | "not_applicable" | null;
  reason: string | null;
  provider: string | null;
  retrieved_at: string | null;
  issued_at: string | null;
  stale: boolean | null;
  used_by_model: boolean;
  values: {
    wind_mph_mean: number | null;
    gust_mph_max: number | null;
    temp_f_mean: number | null;
    precip_prob_max: number | null;
    short_forecast: string[];
  } | null;
};

export type KickerContext = {
  expected_kicker_basis: string;
  designation: string;
  venue: { venue_id: string | null; name: string | null; roof: string | null; elevation_m: number | null; source: string | null; kickoff_utc: string | null };
  weather: Weather;
};

export type Explanation = { feature: string; contribution: number | null; text: string };

export type KickerPlayer = {
  espn_player_id: number;
  name: string | null;
  nfl_team: string | null;
  opponent: string | null;
  is_home: boolean | null;
  fantasy_team_id: number | null;
  injury_status: string | null;
  independent: {
    projected_points: number | null;
    expected_points: number | null;
    p_play: number | null;
    interval_80: { low: number; high: number; nominal_coverage: number } | null;
    coverage_status: string;
    stat_line: StatLine | null;
    context: KickerContext | null;
    explanation: Explanation[];
    flags: string[];
    missing_inputs: string[];
    pregame: boolean | null;
    model_version: string;
  };
  espn: { projected_points: number | null };
  comparison: { expected_minus_espn: number | null };
};

export type KickerResponse = {
  schema_version: string;
  league: { id: number; season: number };
  week: number;
  available: boolean;
  unavailable_reason: string | null;
  governance: { model_version: string; status_note: string; start_sit_ready: boolean; espn_superiority_established: boolean };
  notes: string[];
  run: null | {
    run_id: string;
    model_version: string;
    created_at: string;
    artifact_verified: boolean;
    artifact_note: string | null;
    scoring: { fg_segments: { low: number; high: number | null; points: { made: number; missed: number } }[]; pat: { made: number; missed: number } } | null;
    feature_cutoff: { data_through_season: number | null; data_through_week: number | null };
    forecast_horizon: number;
  };
  players: KickerPlayer[];
};

const STATUS_TEXT: Record<string, string> = {
  bye: "Bye week",
  withheld_unavailable: "Unavailable",
  not_expected_kicker: "Not his team's kicker",
  no_nfl_team: "Not on an NFL roster",
  ambiguous_kicker: "Team kicker unclear",
  uncertain_id_mapping: "ID mapping uncertain",
  unsupported_scoring: "Scoring unsupported",
};

/** Why a kicker has no projection, in a few words. */
export function statusText(status: string): string {
  return STATUS_TEXT[status] ?? status.replace(/_/g, " ");
}

/** Projected kickers first (by expected points), then everyone else by status. */
export function splitPlayers(players: KickerPlayer[]): { projected: KickerPlayer[]; other: KickerPlayer[] } {
  const projected = players
    .filter((p) => p.independent.coverage_status === "projected" && p.independent.expected_points !== null)
    .sort((a, b) => (b.independent.expected_points ?? 0) - (a.independent.expected_points ?? 0));
  const other = players.filter((p) => !projected.includes(p))
    .sort((a, b) => a.independent.coverage_status.localeCompare(b.independent.coverage_status) || (a.name ?? "").localeCompare(b.name ?? ""));
  return { projected, other };
}

const ROOF: Record<string, string> = {
  fixed: "Indoors",
  retractable_unknown: "Retractable roof (position unknown)",
  open_air: "Outdoors",
};

/**
 * One-line venue + weather badge text built only from the values the run stored. It never implies a weather effect
 * the model did not apply: when the forecast is not a model input it says so.
 */
export function conditions(ctx: KickerContext | null): { text: string; detail: string | null; tone: "info" | "warn" | "" } {
  if (!ctx) return { text: "—", detail: null, tone: "" };
  const roof = ROOF[ctx.venue.roof ?? ""] ?? "Venue unknown";
  const elev = ctx.venue.elevation_m !== null && ctx.venue.elevation_m >= 500 ? ` · ${ctx.venue.elevation_m.toLocaleString()} m` : "";
  const w = ctx.weather;
  if (w.values && (w.status === "ok" || w.status === "stale")) {
    const v = w.values;
    const wind = v.wind_mph_mean !== null ? `${Math.round(v.wind_mph_mean)} mph wind` : "wind n/a";
    const gust = v.gust_mph_max !== null ? ` (gusts ${Math.round(v.gust_mph_max)})` : "";
    const temp = v.temp_f_mean !== null ? `, ${Math.round(v.temp_f_mean)}°F` : "";
    const used = w.used_by_model ? "model input" : "shown only, not a model input";
    const stale = w.stale ? " · stale forecast" : "";
    const windy = (v.wind_mph_mean ?? 0) >= 15 || (v.gust_mph_max ?? 0) >= 25;
    return {
      text: `${roof}${elev} · ${wind}${gust}${temp}`,
      detail: `${(w.provider ?? "").toUpperCase()} forecast retrieved ${w.retrieved_at ?? "?"}${stale}; ${used}.`,
      tone: windy ? "warn" : "info",
    };
  }
  return { text: `${roof}${elev}`, detail: w.reason, tone: "" };
}

/** "1.9 FG (0.9 / 0.6 / 0.4 / 0.0) · 2.4 PAT" with makes; null without a stat line. */
export function statLineText(s: StatLine | null): string | null {
  if (!s) return null;
  const by = DISTANCES.map((d) => s.fg_made_by_distance[d.key].toFixed(1)).join(" / ");
  return `${s.fg_made.toFixed(1)}/${s.fg_att.toFixed(1)} FG (${by}) · ${s.pat_made.toFixed(1)}/${s.pat_att.toFixed(1)} PAT`;
}

/** The headline sentence the model wrote, if any. */
export function headline(p: KickerPlayer): string | null {
  return p.independent.explanation.find((e) => e.feature === "headline")?.text ?? null;
}

/** The headline without its "(helped by …; held back by …)" tail, for rows that show the factor chips instead. */
export function outlook(p: KickerPlayer): string | null {
  return headline(p)?.replace(/ \((helped|held back) by [^)]*\)$/, "") ?? null;
}

/** A kicker's row flags, minus the per-row rest-of-season horizon flag: the page states the run's horizon once. */
export function rowFlags(flags: string[]): string[] {
  return flags.filter((f) => !f.startsWith("rest-of-season kicker forecast"));
}

export type FactorChip = { feature: string; label: string; points: number; text: string };

const FACTOR_LABEL: Record<string, string> = {
  pat_opportunity: "TD volume",
  kicker_accuracy: "Kicker accuracy",
  long_range_share: "Long-range tries",
  implied_total: "Implied team total",
};

/** "Dome / altitude", "Dome", "Altitude", "Outdoors" or "Venue", from the stadium the run stored. */
function venueLabel(ctx: KickerContext | null): string {
  const dome = ctx?.venue.roof === "fixed";
  const high = (ctx?.venue.elevation_m ?? 0) >= 500;
  if (dome) return high ? "Dome / altitude" : "Dome";
  if (high) return "Altitude";
  return ctx?.venue.roof === "open_air" ? "Outdoors" : "Venue";
}

/**
 * The model's own point contributions as short chips ("Kicker accuracy +1.5"), biggest first. Each is the change
 * in projected points when that input is set to league average, so the chips explain the gap to an average
 * kicker. Contributions under 0.15 points are left out as noise.
 */
export function factorChips(p: KickerPlayer, max = 4): FactorChip[] {
  return p.independent.explanation
    .filter((e) => e.feature !== "headline" && e.contribution !== null && Math.abs(e.contribution) >= 0.15)
    .sort((a, b) => Math.abs(b.contribution!) - Math.abs(a.contribution!))
    .slice(0, max)
    .map((e) => ({
      feature: e.feature,
      label: e.feature === "venue" ? venueLabel(p.independent.context) : FACTOR_LABEL[e.feature] ?? e.feature.replace(/_/g, " "),
      points: e.contribution!,
      text: e.text,
    }));
}
