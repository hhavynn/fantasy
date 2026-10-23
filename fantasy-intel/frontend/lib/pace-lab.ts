// Types and pure helpers for the pace and game-script lab (GET /api/pace-lab, contract "pace-lab.v1", issue #211). The
// backend owns every number: observed values, shrunk estimates, priors, sample sizes, scenario centres and ranges and
// the frozen backtest. This module reads the URL, builds links and words what the backend sent; a missing value stays
// missing (never 0) and nothing here computes a forecast or a probability.

export const CONTRACT = "pace-lab.v1";
export const PATH = "/pace-lab";
export const SCRIPTS = ["competitive", "leading", "trailing"] as const;
export type Script = (typeof SCRIPTS)[number];
export const SCRIPT_LABEL: Record<Script, string> = { competitive: "Competitive", leading: "Leading", trailing: "Trailing" };
export const SCRIPT_RULE: Record<Script, string> = {
  competitive: "final margin within 8 points",
  leading: "won by 9 or more",
  trailing: "lost by 9 or more",
};
export const WINDOWS = ["season", "last8", "last4"] as const;
export type Window = (typeof WINDOWS)[number];
export const WINDOW_LABEL: Record<Window, string> = { season: "Every game", last8: "Last 8", last4: "Last 4" };
export const SPANS = [1, 2, 3] as const;
export const MEASURES = ["plays", "dropbacks", "designed_rushes", "targets"] as const;
export type Measure = (typeof MEASURES)[number];
export const MEASURE_LABEL: Record<Measure, string> = {
  plays: "Plays", dropbacks: "Dropbacks", designed_rushes: "Designed runs", targets: "Targets",
};

/** Hypothetical mixes offered as one-tap presets (percent: competitive, leading, trailing); null = the observed mix. */
export const PRESETS: readonly { key: string; label: string; mix: readonly [number, number, number] | null }[] = [
  { key: "observed", label: "Observed mix", mix: null },
  { key: "close", label: "Close all game", mix: [100, 0, 0] },
  { key: "lead", label: "Mostly leading", mix: [20, 70, 10] },
  { key: "trail", label: "Mostly trailing", mix: [20, 10, 70] },
];

export type Estimate = {
  observed: number | null;
  estimate: number | null;
  n: number;
  unit: string;
  prior: number | null;
  prior_strength: number;
  data_weight: number | null;
  missing: number;
  excluded: number;
};

export type TeamSummary = {
  games: number;
  missing_pbp: number;
  overtime_games: number;
  plays_per_game: Estimate;
  drives_per_game: Estimate;
  plays_per_drive: Estimate;
  neutral_pass_rate: Estimate;
  pass_rate: Estimate;
  targets_per_dropback: Estimate;
  proe: { observed: number | null; n: number; unit: string };
};

export type ScriptEstimate = { games: number; plays_per_game: Estimate; pass_rate: Estimate };

export type WeekRow = {
  season: number;
  week: number;
  opponent: string | null;
  home: boolean | null;
  listed_as: string | null;
  points_for: number | null;
  points_against: number | null;
  margin: number | null;
  script: Script | null;
  overtime: boolean | null;
  status: "ok" | "missing_pbp" | "overtime";
  plays: number | null;
  drives: number | null;
  plays_per_drive: number | null;
  neutral_plays: number | null;
  neutral_pass_rate: number | null;
  pass_rate: number | null;
  dropbacks: number | null;
  designed_rushes: number | null;
  targets: number | null;
  proe: number | null;
};

export type Range = { expected: number | null; low: number | null; high: number | null };
export type MixResult = { measures: Record<Measure, Range>; range_reason: string | null };

export type OverviewRow = {
  team: string;
  games: number;
  missing_pbp: number;
  overtime_games: number;
  plays_per_game: Estimate;
  drives_per_game: Estimate;
  plays_per_drive: Estimate;
  neutral_pass_rate: Estimate;
  pass_rate: Estimate;
};

export type ModelScore = { mae: number; rmse: number; bias: number };
export type Paired = { mean: number; low: number; high: number };
export type ScoreBlock = {
  n: number;
  models: Record<string, ModelScore>;
  best_baseline?: string;
  comparisons: Record<string, Record<string, Paired>>;
  verdicts: Record<string, "improves" | "worse" | "no clear difference">;
};
export type Coverage = { n: number; inside: number; share: number | null; nominal: number };
export type Backtest =
  | { status: "not_run" | "unreadable"; message: string }
  | {
    status: "frozen";
    version: string;
    spec_sha256: string;
    seasons: number[];
    run_at: string;
    plays: ScoreBlock;
    neutral_pass_rate: ScoreBlock;
    range_coverage: { conditional_on_script: Coverage; pregame_mixture: Coverage };
    data: { games: number; seasons_stored: number[] };
  };

export type League = {
  games: number;
  plays_per_game: number | null;
  drives_per_game: number | null;
  plays_per_drive: number | null;
  neutral_pass_rate: number | null;
  neutral_plays: number;
  pass_rate: number | null;
  targets_per_dropback: number | null;
  scripts: Record<Script, { games: number; share: number | null; plays_per_game: number | null; pass_rate: number | null }>;
};

export type PaceLabResponse = {
  contract: string;
  version: string;
  model_status: string;
  status: "ok" | "no_data";
  message?: string;
  gaps: string[];
  definitions: Record<string, string>;
  available_seasons: number[];
  backtest: Backtest;
  query: {
    season: number; seasons: number[]; span: number; team: string | null; opponent: string | null;
    window: Window; through_week: number | null;
  } | null;
  weeks?: number[];
  teams?: string[];
  source?: {
    tables: string[]; schedule_stored: boolean; drives_stored: boolean; overtime_recorded: boolean;
    neutral_recorded: boolean; nfl_synced_at: string | null; franchise_codes: [string, string][];
  };
  coverage?: {
    games: number; counted: number; missing_pbp: number; not_final: number; overtime: number; overtime_unknown: number;
    no_neutral_play: number; no_drive: number;
  };
  league?: League;
  overview?: OverviewRow[];
  team?: {
    team: string; listed_as: string[]; games: WeekRow[]; window_games: number; summary: TeamSummary;
    scripts: Record<Script, ScriptEstimate>; observed_mix: number[] | null; classified_games: number;
  } | null;
  matchup?: {
    opponent: string; team_plays_per_game: Estimate; opponent_plays_allowed: Estimate; league_plays_per_game: number | null;
    additive_plays: number | null; head_to_head_games: number;
  } | null;
  scenario?: {
    hypothetical: true; input_mix: number[] | null; mix: number[]; baseline_mix: number[];
    baseline_source: "observed" | "league"; baseline: MixResult; result: MixResult;
    change: Record<Measure, { expected: number | null }>;
  } | null;
};

export type SearchParams = Record<string, string | string[] | undefined>;

export type PaceState = {
  season: number | null;
  seasons: 1 | 2 | 3;
  team: string | null;
  opp: string | null;
  window: Window;
  through: number | null;
  /** Percent weights (competitive, leading, trailing), or null for the team's observed mix. */
  mix: [number, number, number] | null;
};

const one = (sp: SearchParams, k: string): string | undefined => {
  const v = sp[k];
  return Array.isArray(v) ? v[0] : v;
};

const intIn = (v: string | undefined, lo: number, hi: number): number | null => {
  if (v === undefined || !/^\d{1,4}$/.test(v)) return null;
  const n = Number(v);
  return n >= lo && n <= hi ? n : null;
};

const teamCode = (v: string | undefined): string | null => (v && /^[A-Za-z]{2,3}$/.test(v) ? v.toUpperCase() : null);

/** The page state from the URL. Anything malformed falls back to the default rather than reaching the backend. */
export function parsePaceState(sp: SearchParams): PaceState {
  const w = one(sp, "window");
  const team = teamCode(one(sp, "team"));
  const opp = team ? teamCode(one(sp, "opp")) : null;
  const parts = ["mc", "ml", "mt"].map((k) => intIn(one(sp, k), 0, 100));
  const anyMix = parts.some((p) => p !== null);
  const mix: [number, number, number] | null = team && anyMix
    ? [parts[0] ?? 0, parts[1] ?? 0, parts[2] ?? 0]
    : null;
  return {
    season: intIn(one(sp, "season"), 1999, 2100),
    seasons: (intIn(one(sp, "seasons"), 1, 3) ?? 1) as 1 | 2 | 3,
    team,
    opp: opp && opp !== team ? opp : null,
    window: (WINDOWS as readonly string[]).includes(w ?? "") ? (w as Window) : "season",
    through: intIn(one(sp, "through"), 1, 18),
    mix: mix && mix.some((x) => x > 0) ? mix : null,
  };
}

/** The backend request for a state. */
export function paceApiPath(s: PaceState): string {
  const q = new URLSearchParams();
  if (s.season !== null) q.set("season", String(s.season));
  if (s.seasons !== 1) q.set("seasons", String(s.seasons));
  if (s.team) q.set("team", s.team);
  if (s.team && s.opp) q.set("opponent", s.opp);
  if (s.window !== "season") q.set("window", s.window);
  if (s.through !== null) q.set("through_week", String(s.through));
  if (s.team && s.mix) {
    q.set("mix_competitive", String(s.mix[0]));
    q.set("mix_leading", String(s.mix[1]));
    q.set("mix_trailing", String(s.mix[2]));
  }
  const qs = q.toString();
  return `/api/pace-lab${qs ? `?${qs}` : ""}`;
}

/** The page URL for a state with some fields changed (LeagueLink scopes it to the page's league). */
export function paceHref(s: PaceState, patch: Partial<PaceState> = {}): string {
  const n = { ...s, ...patch };
  const q = new URLSearchParams();
  if (n.season !== null) q.set("season", String(n.season));
  if (n.seasons !== 1) q.set("seasons", String(n.seasons));
  if (n.team) q.set("team", n.team);
  if (n.team && n.opp) q.set("opp", n.opp);
  if (n.window !== "season") q.set("window", n.window);
  if (n.through !== null) q.set("through", String(n.through));
  if (n.team && n.mix) {
    q.set("mc", String(n.mix[0]));
    q.set("ml", String(n.mix[1]));
    q.set("mt", String(n.mix[2]));
  }
  const qs = q.toString();
  return `${PATH}${qs ? `?${qs}` : ""}`;
}

/** "2024-2025" or "2025". */
export function seasonsText(seasons: number[]): string {
  if (seasons.length === 0) return "";
  return seasons.length === 1 ? String(seasons[0]) : `${seasons[0]}-${seasons[seasons.length - 1]}`;
}

/** A mix as "60% competitive, 20% leading, 20% trailing" (shares 0-1). */
export function mixText(mix: number[]): string {
  return SCRIPTS.map((s, i) => `${Math.round((mix[i] ?? 0) * 100)}% ${s}`).join(", ");
}

/** Whether a preset is the mix in the URL (the observed preset is "no mix"). */
export function presetActive(mix: PaceState["mix"], preset: (typeof PRESETS)[number]): boolean {
  if (preset.mix === null) return mix === null;
  if (mix === null) return false;
  const total = mix[0] + mix[1] + mix[2];
  return preset.mix.every((v, i) => Math.abs(v / 100 - mix[i] / total) < 1e-9);
}

const MODEL_LABEL: Record<string, string> = {
  league_mean: "League mean to date",
  league_rate: "League rate to date",
  team_to_date: "Team season-to-date",
  shrunk_team: "Shrunk team estimate (this lab)",
  matchup_additive: "Team + opponent allowed − league",
  script_mixture: "Script mixture (this lab's scenario centre)",
};

export function modelLabel(key: string): string {
  return MODEL_LABEL[key] ?? key.replaceAll("_", " ");
}

export const VERDICT_TEXT: Record<string, string> = {
  improves: "Beat both simple baselines",
  worse: "Worse than the better simple baseline",
  "no clear difference": "No clear difference from the better simple baseline",
};

/**
 * One factual sentence per backtested estimate, used beside the assumptions it judges. Words only what the frozen
 * result says; with no result it says no claim is made.
 */
export function backtestSentences(bt: Backtest): { pace: string; neutral: string; matchup: string; scenario: string } {
  if (bt.status !== "frozen") {
    const none = "Not backtested yet: no claim is made about forecast value.";
    return { pace: none, neutral: none, matchup: none, scenario: none };
  }
  const span = seasonsText(bt.seasons);
  const say = (block: ScoreBlock, key: string, what: string): string => {
    const v = block.verdicts[key];
    const base = block.best_baseline ? modelLabel(block.best_baseline).toLowerCase() : "the simple baselines";
    if (v === "improves") return `In the ${span} backtest, ${what} forecast the next game better than both simple baselines.`;
    if (v === "worse") return `In the ${span} backtest, ${what} forecast the next game worse than the ${base}.`;
    if (v === "no clear difference") return `In the ${span} backtest, ${what} did not forecast the next game better than the ${base}.`;
    return `${what} was not backtested.`;
  };
  return {
    pace: say(bt.plays, "shrunk_team", "the shrunk plays per game"),
    neutral: say(bt.neutral_pass_rate, "shrunk_team", "the shrunk neutral pass rate"),
    matchup: say(bt.plays, "matchup_additive", "the additive opponent adjustment"),
    scenario: say(bt.plays, "script_mixture", "the script-mix centre"),
  };
}

/** "81.6% of 3,120 games (nominal 80%)" for a range coverage, or null. */
export function coverageText(c: Coverage | undefined): string | null {
  if (!c || c.share === null || c.n === 0) return null;
  return `${(c.share * 100).toFixed(1)}% of ${c.n.toLocaleString("en-US")} games (nominal ${Math.round(c.nominal * 100)}%)`;
}

/** Why a value is missing, in words, for the `—` tooltip. */
export function missingWhy(row: WeekRow, field: "plays" | "drives" | "neutral" | "pass"): string {
  if (row.status === "missing_pbp") return "No play-by-play aggregate is stored for this game";
  if (field === "drives") return "No drive row is stored for this game";
  if (field === "neutral") return "No neutral-situation play in this game: missing, not 0%";
  return "Not stored";
}

/** Score line "W 27-20" / "L 10-31" / "T 20-20", or null. */
export function resultText(row: WeekRow): string | null {
  if (row.points_for === null || row.points_against === null) return null;
  const tag = row.points_for > row.points_against ? "W" : row.points_for < row.points_against ? "L" : "T";
  return `${tag} ${row.points_for}-${row.points_against}`;
}
