// Pure helpers for the Playoff race page: the URL-backed scenario state and where the cutline sits
// in a standings list. Nothing here computes a standings number; it only arranges backend output.
import type { PrStatus } from "@/lib/types";

export type RaceQuery = {
  /** Spotlight team id (as in the URL), if chosen. */
  team?: string;
  base: "projected" | "actual";
  /** Winner overrides by matchup id: "home" | "away" | "tie" | "none". */
  o: Record<string, string>;
};

type Param = string | string[] | undefined;

const many = (v: Param): string[] => (v === undefined ? [] : Array.isArray(v) ? v : [v]);

/** Read the scenario from search params. Malformed `o` entries (no `id:outcome`) are ignored. */
export function parseRaceQuery(sp: Record<string, Param>): RaceQuery {
  const o: Record<string, string> = {};
  for (const v of many(sp.o)) {
    const [mid, out] = v.split(":");
    if (mid && out) o[mid] = out;
  }
  return { team: many(sp.team)[0], base: many(sp.base)[0] === "actual" ? "actual" : "projected", o };
}

/** The page URL for a scenario. The default baseline and an empty override set add nothing to the query. */
export function raceHref(q: RaceQuery, change: Partial<RaceQuery> = {}): string {
  const n = { ...q, ...change };
  const p = new URLSearchParams();
  if (n.team) p.set("team", n.team);
  if (n.base !== "projected") p.set("base", n.base);
  Object.entries(n.o).sort(([a], [b]) => Number(a) - Number(b)).forEach(([mid, out]) => p.append("o", `${mid}:${out}`));
  const s = p.toString();
  return `/playoff-race${s ? `?${s}` : ""}`;
}

/** The URL with one matchup's override set (or removed when `outcome` is null). */
export function withOverride(q: RaceQuery, matchupId: number, outcome: string | null): string {
  const o = { ...q.o };
  if (outcome === null) delete o[String(matchupId)];
  else o[String(matchupId)] = outcome;
  return raceHref(q, { o });
}

/** Query string the backend takes for a scenario (`team_id`, `base`, repeated `override`). */
export function backendQuery(q: RaceQuery): string {
  const p = new URLSearchParams();
  if (q.team) p.set("team_id", q.team);
  p.set("base", q.base);
  Object.entries(q.o).forEach(([mid, out]) => p.append("override", `${mid}:${out}`));
  return p.toString();
}

/**
 * Index of the row BEFORE which the playoff line is drawn: the number of rows with status
 * "inside" (they come first in the order), so the line lands after the last spot or above a tied
 * group that straddles it. Null when playoff spots are unknown or no row has a status at all
 * (nothing establishes a cutline position yet).
 */
export function cutlineIndex(rows: readonly { status: PrStatus }[], playoffSpots: number | null): number | null {
  if (playoffSpots === null) return null;
  if (rows.every((x) => x.status === null)) return null;
  return rows.filter((x) => x.status === "inside").length;
}

export type WithCutline<T> = { kind: "row"; row: T; index: number } | { kind: "cutline" };

/** `rows` with a cutline marker inserted before row `after`. Not inserted when `after` is null or past the last row. */
export function withCutline<T>(rows: readonly T[], after: number | null): WithCutline<T>[] {
  const out: WithCutline<T>[] = [];
  rows.forEach((row, index) => {
    if (after !== null && index === after) out.push({ kind: "cutline" });
    out.push({ kind: "row", row, index });
  });
  return out;
}

/** "wk 7" or "wk 7–8" for a (possibly multi-week) matchup; falls back to the period. */
export function weeksText(weeks: number[] | null | undefined, period: number): string {
  return weeks && weeks.length ? (weeks.length > 1 ? `wk ${weeks[0]}–${weeks[weeks.length - 1]}` : `wk ${weeks[0]}`) : `period ${period}`;
}

// ------------------------------------------------------------------ what-if (issue #192) --

type PlaceLike = { rank: number | null; status: PrStatus; record: { wins: number; losses: number; ties: number; unresolved?: number } | null };

/** A team's what-if row differs from the baseline: its cutline status, its rank or its record (unresolved count included). */
export function whatifChanged(c: { baseline: PlaceLike; whatif: PlaceLike }): boolean {
  const a = c.baseline, b = c.whatif;
  if (a.status !== b.status || a.rank !== b.rank) return true;
  if (!a.record || !b.record) return a.record !== b.record;
  return a.record.wins !== b.record.wins || a.record.losses !== b.record.losses || a.record.ties !== b.record.ties
    || (a.record.unresolved ?? 0) !== (b.record.unresolved ?? 0);
}

/** Why remaining games are still unresolved in a what-if, in words. */
export const UNRESOLVED_REASON: Record<string, string> = {
  set_unresolved: "set unresolved by you",
  in_progress: "under way",
  unmapped: "weeks unknown",
  actual_baseline: "no result yet (actual-results baseline)",
  projection_incomplete: "projection incomplete",
  equal_projection: "equal projections",
};

/** "2 projection incomplete, 1 under way": counts per reason, largest first, unknown reasons kept as given. */
export function unresolvedText(byReason: Record<string, number>): string {
  return Object.entries(byReason)
    .filter(([, n]) => n > 0)
    .sort(([a, x], [b, y]) => y - x || a.localeCompare(b))
    .map(([k, n]) => `${n} ${UNRESOLVED_REASON[k] ?? k.replace(/_/g, " ")}`)
    .join(", ");
}

/**
 * The Matchup page for a game whose result was set: the game's first scoring week, from the spotlight team's side
 * when it plays in the game, else the home side. No week when the game's weeks are unknown.
 */
export function matchupAnalysisHref(
  m: { weeks: number[] | null; home_team_id: number; away_team_id: number }, focus: number | null,
): string {
  const p = new URLSearchParams();
  if (m.weeks && m.weeks.length) p.set("week", String(m.weeks[0]));
  p.set("team", String(focus !== null && (focus === m.home_team_id || focus === m.away_team_id) ? focus : m.home_team_id));
  return `/matchup?${p}`;
}
