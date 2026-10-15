// Pure view helpers for the Managers pages. They choose and arrange stored classifications and
// weekly counts; no label, metric or count is computed here.

export const MANAGER_TABS = ["overview", "scouting", "transactions"] as const;
export type ManagerTab = (typeof MANAGER_TABS)[number];

/** The active tab from `?tab=`; anything unknown (or missing) is the overview. */
export function parseTab(v: string | string[] | undefined): ManagerTab {
  const raw = Array.isArray(v) ? v[0] : v;
  return (MANAGER_TABS as readonly string[]).includes(raw ?? "") ? (raw as ManagerTab) : "overview";
}

type Labelled = { dimension: string; label: string; confidence: string };

const CONFIDENCE_RANK: Record<string, number> = { HIGH: 0, MEDIUM: 1, LOW: 2 };

/**
 * The tendencies worth showing on a card: stored classifications labelled HIGH (league-relative
 * outliers), most confident first, ties in `order` (the display order of dimensions). A tendency
 * with INSUFFICIENT history is never a standout.
 */
export function standoutTendencies<T extends Labelled>(classifications: Record<string, T> | readonly T[], order: readonly string[], limit = 4): T[] {
  const rows = Array.isArray(classifications) ? [...classifications] : Object.values(classifications);
  const pos = (d: string) => { const i = order.indexOf(d); return i === -1 ? order.length : i; };
  return rows
    .filter((c) => c.label === "HIGH" && c.confidence !== "INSUFFICIENT")
    .sort((a, b) => (CONFIDENCE_RANK[a.confidence] ?? 3) - (CONFIDENCE_RANK[b.confidence] ?? 3) || pos(a.dimension) - pos(b.dimension))
    .slice(0, limit);
}

/** How many stored classifications cannot be judged yet (label INSUFFICIENT). */
export function insufficientCount(classifications: Record<string, Labelled> | readonly Labelled[]): number {
  const rows = Array.isArray(classifications) ? classifications : Object.values(classifications);
  return rows.filter((c) => c.label === "INSUFFICIENT").length;
}

export type CompositionRow = { scoring_period: number; position: string; n: number; starters: number };

/**
 * Roster size per position per scoring period, for a line chart. A period with no row for a
 * position means none of that position was rostered (the backend groups only what exists), so it
 * is a measured zero. Periods are the ones present in the data, ascending.
 */
export function compositionSeries(rows: readonly CompositionRow[], positions: readonly string[]) {
  const periods = [...new Set(rows.map((r) => r.scoring_period))].sort((a, b) => a - b);
  const byKey = new Map(rows.map((r) => [`${r.scoring_period}|${r.position}`, r]));
  return {
    periods,
    series: positions.map((position) => ({
      position,
      points: periods.map((p) => byKey.get(`${p}|${position}`)?.n ?? 0),
    })),
  };
}

type Standable = { team_id: number; wins: number; losses: number; ties: number; points_for: number | null; playoff_seed: number | null };

/**
 * Teams in standing order, best first. The API lists teams by team_id, so order is derived: by ESPN's
 * `playoff_seed` when every team has one, otherwise by win share (a tie counts half), then points
 * for, then team_id so the order is stable.
 */
export function standingOrder<T extends Standable>(teams: readonly T[]): T[] {
  const seeded = teams.length > 0 && teams.every((t) => t.playoff_seed !== null && t.playoff_seed !== undefined);
  return [...teams].sort((a, b) => {
    if (seeded) return (a.playoff_seed as number) - (b.playoff_seed as number) || a.team_id - b.team_id;
    const wa = winShare(a), wb = winShare(b);
    return (wb ?? -1) - (wa ?? -1) || (b.points_for ?? -1e9) - (a.points_for ?? -1e9) || a.team_id - b.team_id;
  });
}

/** Win share for sorting only (a tie counts half); no games yet is missing. */
export function winShare(t: { wins: number; losses: number; ties: number }): number | null {
  const g = t.wins + t.losses + t.ties;
  return g === 0 ? null : (t.wins + t.ties / 2) / g;
}
