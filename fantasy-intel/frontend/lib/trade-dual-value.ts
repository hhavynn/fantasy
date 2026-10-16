// Pure helpers for Trade Discovery 2.0 (dual value): the lens tabs, verdict and check presentation, the
// fairness/downside label, weekly lineup-change rows and rating text. No React; unit-tested in
// lib/trade-dual-value.test.ts. The vocabulary is the backend's (analytics/trade_dual_value.py,
// docs/trade_discovery_dual_value.md). Nothing here estimates whether a manager would accept a trade.

import type { CheckStatus, DName, DVCheckBrief, DVRating, DVWeekly } from "./trade-types.ts";

// ------------------------------------------------------------------------------------------ lenses --

/** The page's default lens. The API's own default stays `practical`, so `discoverQuery` sends this explicitly. */
export const DEFAULT_MODE = "mutual_upgrades";

export type LensTab = { mode: string; label: string; hint?: string };

/** The lens tabs, in display order. `all` is the advanced view. */
export const LENSES: readonly LensTab[] = [
  { mode: "mutual_upgrades", label: "Best mutual upgrades" },
  { mode: "fair", label: "Fairer asset exchanges" },
  { mode: "upside", label: "Maximum upside", hint: "exploratory" },
  { mode: "consolidation", label: "Consolidation deals" },
  { mode: "playoff", label: "Playoff-focused trades" },
  { mode: "all", label: "All candidates", hint: "advanced" },
];

/** Older deep links keep working; their view is shown as one extra tab so the current view is always visible. */
export const LEGACY_LENSES: Record<string, LensTab> = {
  practical: { mode: "practical", label: "Practical (classic)" },
  mutual: { mode: "mutual", label: "Mutual improvement (classic)" },
};

export const isLegacyMode = (mode: string | null | undefined): boolean => !!mode && Object.hasOwn(LEGACY_LENSES, mode);

/** The tabs to render for the active mode: the six lenses, plus the classic tab when a legacy mode is active. */
export function lensTabs(current: string | null | undefined): LensTab[] {
  return isLegacyMode(current) ? [...LENSES, LEGACY_LENSES[current as string]] : [...LENSES];
}

/** Tab text: the label, plus its hint (" · advanced") where the lens has one. */
export const lensTabText = (t: LensTab): string => (t.hint ? `${t.label} · ${t.hint}` : t.label);

/** The phase filter applies to lenses that pick a phase themselves only when this is true (upside, all and playoff set their own). */
export const modeUsesPhase = (mode: string): boolean => !["upside", "all", "playoff"].includes(mode);
/** The asset-balance filter (strictness) is a classic practical-mode option. */
export const modeUsesStrictness = (mode: string): boolean => mode === "practical";

// ---------------------------------------------------------------------------------------- verdicts --

/** Verdicts in the backend's ranking order (`trade_dual_value.VERDICTS`): a lower index ranks first. */
export const VERDICT_ORDER = ["sensible", "sensible_with_concerns", "unlikely_mutual", "fair_no_lineup_gain", "one_sided", "incomplete"] as const;

export type Tone = "good" | "warn" | "bad" | "";

const VERDICT_INFO: Record<string, { label: string; tone: Tone }> = {
  sensible: { label: "Sensible for both", tone: "good" },
  sensible_with_concerns: { label: "Sensible, with concerns", tone: "warn" },
  unlikely_mutual: { label: "Unlikely to be mutual", tone: "bad" },
  fair_no_lineup_gain: { label: "Fair, no lineup gain", tone: "warn" },
  one_sided: { label: "One-sided", tone: "bad" },
  incomplete: { label: "Incomplete", tone: "" },
};

/** Badge text and tone for a verdict code. An unknown code (a newer backend) shows its code, neutral. */
export function verdictInfo(code: string | null | undefined): { label: string; tone: Tone } {
  if (!code) return { label: "—", tone: "" };
  return VERDICT_INFO[code] ?? { label: code.replaceAll("_", " "), tone: "" };
}

/** Rank of a verdict in the backend order; `null` (sorts last) for a missing or unknown verdict. */
export function verdictRank(code: string | null | undefined): number | null {
  const i = VERDICT_ORDER.indexOf(code as (typeof VERDICT_ORDER)[number]);
  return i < 0 ? null : i;
}

// ------------------------------------------------------------------------------------------ checks --

/** Short chip names for the six plausibility criteria, in the backend's order. */
export const CRITERIA_ORDER = ["lineup_gain", "asset_balance", "positional_fit", "depth_cost", "free_agent_alternative", "uncertainty"] as const;
export const CRITERION_SHORT: Record<string, string> = {
  lineup_gain: "Lineups", asset_balance: "Assets", positional_fit: "Fit", depth_cost: "Depth",
  free_agent_alternative: "vs free agents", uncertainty: "Certainty",
};
/** Used when a payload carries no `criteria` map (the backend's own texts win). */
export const CRITERION_TEXT: Record<string, string> = {
  lineup_gain: "Both projected starting lineups improve (starters only; bench points never count).",
  asset_balance: "The asset packages are reasonably balanced (points above replacement).",
  positional_fit: "Each side receives a player who starts at least half of the remaining weeks.",
  depth_cost: "No starter-caliber player is forced off a roster and no starting slot is left empty.",
  free_agent_alternative: "The trade adds more than the best available free agent would (your side).",
  uncertainty: "No injury, eligibility or missing-projection uncertainty on the traded players.",
};

const STATUS_INFO: Record<CheckStatus, { glyph: string; label: string; tone: Tone }> = {
  pass: { glyph: "✓", label: "Pass", tone: "good" },
  concern: { glyph: "!", label: "Concern", tone: "warn" },
  fail: { glyph: "✕", label: "Fail", tone: "bad" },
  unknown: { glyph: "?", label: "Unknown", tone: "" },
  not_applicable: { glyph: "–", label: "Not applicable", tone: "" },
};

/** Glyph, word and tone for a check status. The glyph and the word carry the meaning; colour only reinforces it. */
export function checkStatusInfo(status: string | null | undefined): { glyph: string; label: string; tone: Tone } {
  return STATUS_INFO[status as CheckStatus] ?? { glyph: "?", label: "Unknown", tone: "" };
}

/** The six checks in the backend's order (a payload that omits one simply has fewer chips). */
export function orderedChecks<T extends DVCheckBrief>(checks: readonly T[] | null | undefined): T[] {
  const list = checks ?? [];
  const idx = (c: string) => { const i = (CRITERIA_ORDER as readonly string[]).indexOf(c); return i < 0 ? CRITERIA_ORDER.length : i; };
  return [...list].sort((a, b) => idx(a.criterion) - idx(b.criterion));
}

/** Accessible description of a chip: "Lineups: Pass. <criterion text>". */
export function checkTitle(c: DVCheckBrief & { text?: string }, criteria?: Record<string, string> | null): string {
  const name = CRITERION_SHORT[c.criterion] ?? c.criterion.replaceAll("_", " ");
  const what = c.text ?? criteria?.[c.criterion] ?? CRITERION_TEXT[c.criterion] ?? "";
  return `${name}: ${checkStatusInfo(c.status).label}.${what ? ` ${what}` : ""}`;
}

// ------------------------------------------------------------------------------ balance and concern --

/** "Favours you" / "Favours them" / "Favours <name>"; `null` when the package is balanced (no `favours`). */
export function favoursText(favours: string | null | undefined): string | null {
  if (!favours) return null;
  const f = favours.trim().toLowerCase();
  if (f === "you") return "Favours you";
  if (f === "they" || f === "them") return "Favours them";
  return `Favours ${favours}`;
}

/**
 * The check that `main_concern` comes from: the backend takes the first failed check, else the first concern, in the
 * criteria order. `null` when there is none (or no checks, as on an older backend).
 */
export function concernSource(checks: readonly DVCheckBrief[] | null | undefined): string | null {
  const ordered = orderedChecks(checks);
  return (ordered.find((c) => c.status === "fail") ?? ordered.find((c) => c.status === "concern"))?.criterion ?? null;
}

/**
 * The prominent label for `main_concern`: "Fairness concern" when the concern is the asset check (the packages are
 * lopsided), otherwise "Downside". `null` without a concern. A lineup failure next to an asset concern is still a
 * downside, because the text shown is the lineup text.
 */
export function concernLabel(checks: readonly DVCheckBrief[] | null | undefined, mainConcern: string | null | undefined): string | null {
  if (!mainConcern) return null;
  return concernSource(checks) === "asset_balance" ? "Fairness concern" : "Downside";
}

// ---------------------------------------------------------------------------------------- numbers --

/** `1,210.3` style points, `—` when missing (never 0). */
export function pointsText(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

/** "1,210.3 → 1,225.0": a side's projected starting points before and after (a missing end shows "—"). */
export function beforeAfterText(before: number | null | undefined, after: number | null | undefined): string {
  return `${pointsText(before)} → ${pointsText(after)}`;
}

/** A rating as `xx.x/100`; a missing rating is "—", never 0. */
export function ratingText(v: number | null | undefined): string {
  return v === null || v === undefined || Number.isNaN(v) ? "—" : `${v.toFixed(1)}/100`;
}

/** The three ratings a player has, in display order. A player with no stored rating has all three as "—". */
export function ratingFields(r: DVRating | null | undefined): { key: string; label: string; text: string }[] {
  return [
    { key: "opportunity_score_100", label: "Opportunity", text: ratingText(r?.opportunity_score_100) },
    { key: "production_score_100", label: "Production", text: ratingText(r?.production_score_100) },
    { key: "value_score_100", label: "Value", text: ratingText(r?.value_score_100) },
  ];
}

/** "64%" for a 0-1 asset-package ratio (the smaller package as a share of the larger); "—" when unknown. */
export function ratioText(v: number | null | undefined): string {
  return v === null || v === undefined || Number.isNaN(v) ? "—" : `${Math.round(v * 100)}%`;
}

/** Points above replacement, one decimal; "—" when the player's value is unknown (never 0). */
export function parText(v: number | null | undefined): string {
  return v === null || v === undefined || Number.isNaN(v) ? "—" : v.toFixed(1);
}

/** "wk 11, 12" / "wk 14"; "—" for none. */
export function weeksText(weeks: readonly number[] | null | undefined): string {
  return weeks && weeks.length ? `wk ${weeks.join(", ")}` : "—";
}

type Role = { role_before: string | null; role_after: string | null; starts_before: number | null; starts_after: number | null; weeks: number };

/**
 * A traded player's role for the sender before and the receiver after: "part-time → starter" and "2/4 → 3/4 weeks".
 * No receiver role (`starts_after` null) means the receiver has to drop him; unknown ends are "—".
 */
export function roleChangeText(p: Role): { roles: string; starts: string } {
  const dropped = p.starts_after === null && p.role_after === null;
  const n = p.weeks;
  const frac = (k: number | null) => (k === null || !n ? "—" : `${k}/${n}`);
  return {
    roles: `${p.role_before ?? "—"} → ${dropped ? "forced drop" : p.role_after ?? "—"}`,
    starts: `${frac(p.starts_before)} → ${dropped ? "—" : frac(p.starts_after)} weeks`,
  };
}

// -------------------------------------------------------------------------------- weekly lineup rows --

/** A player's name from the evidence `names` map, or `#id` when it is missing. */
export function playerLabel(id: number, names: Record<string, DName | undefined> | null | undefined): string {
  return names?.[String(id)]?.name || `#${id}`;
}

const PHASE_TEXT: Record<string, string> = { regular: "regular season", playoff: "playoffs", playoffs: "playoffs" };

export type WeeklyRow = {
  week: number; phase: string; delta: number | null; deltaText: string; entered: string[]; exited: string[]; changed: boolean;
};

/** One week of a side's lineup changes with names resolved. A missing delta is "—" (never 0); a missing name is `#id`. */
export function weeklyChangeRow(w: DVWeekly, names: Record<string, DName | undefined> | null | undefined): WeeklyRow {
  const delta = typeof w.delta === "number" && !Number.isNaN(w.delta) ? w.delta : null;
  const entered = (w.entered ?? []).map((id) => playerLabel(id, names));
  const exited = (w.exited ?? []).map((id) => playerLabel(id, names));
  return {
    week: w.week,
    phase: w.phase ? PHASE_TEXT[w.phase] ?? w.phase.replaceAll("_", " ") : "—",
    delta,
    deltaText: delta === null ? "—" : `${delta > 0 ? "+" : delta < 0 ? "−" : ""}${Math.abs(delta).toFixed(1)}`,
    entered, exited, changed: entered.length + exited.length > 0,
  };
}

// --------------------------------------------------------------------------------------- sections --

/** True when the response's sections are verdict groups (a dual-value lens) rather than the classic tiers or `results`. */
export function sectionsAreVerdicts(keys: readonly string[], verdicts?: Record<string, string> | null): boolean {
  return keys.length > 0 && keys.every((k) => k in VERDICT_INFO || (!!verdicts && k in verdicts));
}

/** "3 lopsided, 1 one sided" from the backend's `excluded` counts (zero and missing counts are left out). */
export function excludedText(excluded: Record<string, number | undefined> | null | undefined): string {
  return Object.entries(excluded ?? {})
    .filter(([, n]) => typeof n === "number" && n > 0)
    .map(([k, n]) => `${n} ${k.replaceAll("_", " ")}`)
    .join(", ");
}
