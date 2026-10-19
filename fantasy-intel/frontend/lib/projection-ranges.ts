// Floor / ceiling ranges around the Ours projection (GET /api/projections/ranges, backend
// app/analytics/projection_ranges.py). Floor = 10th percentile, ceiling = 90th percentile of a player's points if
// he plays: 80% of outcomes land between them. RB, WR and TE carry a range; QB, K and D/ST do not.
// Pure functions so the formatting and lookups are testable; the fetch lives in projection-ranges.server.ts.

export type PlayerRange = {
  position: string;
  projection: number;
  floor: number;
  median: number;
  ceiling: number;
};

export type RangesResponse = {
  schema_version: string;
  available: boolean;
  reason: string | null;
  season: number;
  week: number;
  coverage: number;
  definition: string;
  positions: Record<string, { served: boolean; reason: string | null }>;
  run: { run_id: string; model_version: string; forecast_horizon: number } | null;
  ranges: Record<string, PlayerRange>;
};

/** Player id -> range for one week; empty when nothing is served. */
export type RangeMap = ReadonlyMap<number, PlayerRange>;

export const EMPTY_RANGES: RangeMap = new Map();

export const RANGE_TITLE = "Floor to ceiling: his 10th to 90th percentile outcome if he plays (80% of games land inside)";

export const RANGE_FOOTNOTE = "Range under an Ours projection: floor to ceiling, the 10th to 90th percentile of his points if he plays (RB, WR, TE).";

export function rangeMap(resp: RangesResponse | null | undefined): RangeMap {
  if (!resp || !resp.available || !resp.ranges) return EMPTY_RANGES;
  const out = new Map<number, PlayerRange>();
  for (const [id, r] of Object.entries(resp.ranges)) {
    const n = Number(id);
    if (Number.isInteger(n) && Number.isFinite(r.floor) && Number.isFinite(r.ceiling)) out.set(n, r);
  }
  return out;
}

/** The range for a player, only when the number shown next to it is our model's (origin "ours"). */
export function rangeFor(ranges: RangeMap, id: number | null | undefined, origin?: string | null): PlayerRange | null {
  if (id === null || id === undefined) return null;
  if (origin !== undefined && origin !== "ours") return null;
  return ranges.get(id) ?? null;
}

const one = (v: number) => (Math.round(v * 10) / 10).toFixed(1);

/** "5.1–24.8" (en dash), the compact form under a projection. */
export function rangeText(r: Pick<PlayerRange, "floor" | "ceiling">): string {
  return `${one(r.floor)}–${one(r.ceiling)}`;
}

/** "Floor 5.1, ceiling 24.8" for screen readers and wider layouts. */
export function rangeLabel(r: Pick<PlayerRange, "floor" | "ceiling">): string {
  return `Floor ${one(r.floor)}, ceiling ${one(r.ceiling)}`;
}
