// Pure helpers for the My team value page. Everything here sorts, filters or formats numbers the
// backend already computed; nothing derives a new analytic.
import type { PrRecord, TvPhase, TvPhaseSplit, TvPlayer } from "./types";

export const PHASE_TEXT: Record<TvPhase, string> = {
  all: "Rest of season",
  regular: "Regular season",
  playoff: "Fantasy playoffs",
};

/** Reads `?phase=`; anything but regular/playoff is the full window. */
export function parsePhase(v: string | undefined): TvPhase {
  return v === "regular" || v === "playoff" ? v : "all";
}

/** Exact lineup marginal for a window, or null when a lineup-relevant week is unprojected. */
export function marginalFor(p: TvPlayer, phase: TvPhase): number | null {
  return phase === "all" ? p.marginal.all : phase === "regular" ? p.marginal.regular : p.marginal.playoff;
}

/** Partial marginal over the projected weeks only (shown labelled "known", never as exact). */
export function knownMarginalFor(p: TvPlayer, phase: TvPhase): number | null {
  return phase === "all" ? p.marginal.known_all : phase === "regular" ? p.marginal.known_regular : p.marginal.known_playoff;
}

/** Value used to order players: exact when available, else the known partial, else missing. */
export function marginalSortValue(p: TvPlayer, phase: TvPhase): number | null {
  return marginalFor(p, phase) ?? knownMarginalFor(p, phase);
}

export interface Contributor {
  name: string;
  value: number;
  exact: boolean;
}

/** The `n` players who move the lineup most in the window, largest first. Partial values are flagged. */
export function topContributors(players: readonly TvPlayer[], phase: TvPhase, n: number): Contributor[] {
  return players
    .map((p) => ({ p, value: marginalSortValue(p, phase) }))
    .filter((x): x is { p: TvPlayer; value: number } => x.value !== null && x.value > 0)
    .sort((a, b) => b.value - a.value || a.p.espn_player_id - b.p.espn_player_id)
    .slice(0, n)
    .map(({ p, value }) => ({ name: p.name ?? `ESPN #${p.espn_player_id}`, value, exact: marginalFor(p, phase) !== null }));
}

/** "W-L" or "W-L-T"; a missing record is a dash. */
export function recordText(r: PrRecord | null | undefined): string {
  return r ? `${r.wins}-${r.losses}${r.ties ? `-${r.ties}` : ""}` : "—";
}

export type Params = Record<string, string | string[] | undefined>;

/**
 * Link to this page with `changes` applied to the current search params. A `null` change removes
 * the param. Every other param (team, sort, dir, trades) is kept.
 */
export function withParams(path: string, sp: Params, changes: Record<string, string | null>): string {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(sp)) {
    if (k in changes || v === undefined) continue;
    for (const item of Array.isArray(v) ? v : [v]) qs.append(k, item);
  }
  for (const [k, v] of Object.entries(changes)) if (v !== null) qs.set(k, v);
  const s = qs.toString();
  return s ? `${path}?${s}` : path;
}

export type PhaseState = "empty" | "incomplete" | "complete";

/**
 * Whether a phase total is usable. A phase with no weeks left (e.g. the regular season while the
 * fantasy playoffs are under way) is "empty", not incomplete: there is nothing to project. A phase
 * with weeks but no exact total is "incomplete" (only a partial known sum exists).
 */
export function phaseState(s: Pick<TvPhaseSplit, "weeks" | "projected_points">): PhaseState {
  if (s.weeks.length === 0) return "empty";
  return s.projected_points === null ? "incomplete" : "complete";
}
