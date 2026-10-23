// Roster utility (#216) on My Team Value: the GET /api/my-team/value/marginal contract and the pure helpers its section
// renders with. Every number is the backend's exact lineup difference; nothing here adds marginal values up.

export const MAX_ASSETS = 3;
export const POOLS = ["bench", "waiver"] as const;
export type RosterPool = (typeof POOLS)[number];

export type UtilityWeek = {
  week: number; phase: string | null; delta: number | null; known_delta: number;
  entered_lineup: number[]; left_lineup: number[];
};

export type UtilityChange = {
  ros_delta: number | null; known_ros_delta: number; regular_season_delta: number | null; playoff_delta: number | null;
  weeks_incomplete: number[]; projection_complete: boolean; weeks: UtilityWeek[];
};

export type RosterUtility = {
  contract: "roster-utility/v1";
  asset_ids: number[]; pool: RosterPool; unit: string; projection_source: string;
  horizon: { weeks: number[]; start_week: number; final_week: number; last_regular_season_week: number | null; note: string };
  reserved: number[]; reserved_note?: string;
  replacement: { espn_player_id: number; name: string | null; position: string | null; note?: string } | null;
  status: "evaluated" | "unknown_pool" | "partial_pool"; reason: string | null;
  removal: UtilityChange; change: UtilityChange | null;
  single_deltas?: Record<string, number | null>; sum_of_single_deltas?: number | null; nonadditive?: boolean | null;
  nonadditive_note?: string;
  waiver_candidates?: { scored: number; excluded_unprojected: number; excluded_rostered: number };
  waiver_availability?: { free_agents: number; availability_known: number; excluded_unknown_availability: number;
    excluded_position_limit: number };
  baseline: { ros_projected_starting_points: number | null; ros_known_starting_points: number; projection_complete: boolean };
  streaming: { enabled: boolean; note?: string };
  team: { team_id: number; name: string; is_mine: boolean };
  names: Record<string, { name: string | null; position: string | null; pro_team: string | null }>;
};

/** The players to remove: ids on this roster, once each, in the order chosen, at most three. */
export function parseAssets(raw: readonly number[], roster: readonly number[]): number[] {
  const on = new Set(roster);
  return [...new Set(raw.filter((id) => on.has(id)))].slice(0, MAX_ASSETS);
}

export function parsePool(raw: string | undefined): RosterPool {
  return raw === "waiver" ? "waiver" : "bench";
}

/** The backend query for a question (the team only when the page is for another team). */
export function utilityQuery(ids: readonly number[], pool: RosterPool, team: string | undefined): string {
  const q = new URLSearchParams();
  ids.forEach((id) => q.append("ids", String(id)));
  if (pool !== "bench") q.set("pool", pool);
  if (team) q.set("team_id", team);
  return q.toString();
}

/** A waiver replacement is one player at a time; with more chosen, the bench answers. */
export function effectivePool(ids: readonly number[], pool: RosterPool): RosterPool {
  return ids.length > 1 ? "bench" : pool;
}

export const nameOf = (r: Pick<RosterUtility, "names">, id: number) => r.names[String(id)]?.name ?? `Player ${id}`;

/** Who left and who entered the starting lineup one week, in words. */
export function pathText(r: Pick<RosterUtility, "names">, w: Pick<UtilityWeek, "entered_lineup" | "left_lineup">): string {
  if (w.left_lineup.length === 0 && w.entered_lineup.length === 0) return "No lineup change";
  const left = w.left_lineup.map((id) => nameOf(r, id)).join(", ");
  const entered = w.entered_lineup.map((id) => nameOf(r, id)).join(", ");
  if (!entered) return `${left} out; nobody fills in`;
  if (!left) return `${entered} in`;
  return `${left} out; ${entered} in`;
}

/** K and D/ST waiver streamers have these ids in a lineup path (team_lineups.STREAMER_IDS). */
export const STREAMER_IDS = [10 ** 12 + 1, 10 ** 12 + 2];

/** A weekly K/D/ST stream filled in somewhere along the path (a waiver fill-in, even from the bench pool). */
export function usesStream(c: { weeks: Pick<UtilityWeek, "entered_lineup">[] } | null): boolean {
  return !!c && c.weeks.some((w) => w.entered_lineup.some((id) => STREAMER_IDS.includes(id)));
}

/** The question asked, in words: who fills in names the stream when a K/D/ST streamer does. */
export function questionText(r: Pick<RosterUtility, "names" | "asset_ids" | "pool">, streamed = false): string {
  const who = r.asset_ids.map((id) => nameOf(r, id)).join(" and ");
  if (r.pool === "waiver") return `Without ${who}, adding the best available free agent`;
  return streamed ? `Without ${who}, from your bench or a weekly K/D/ST waiver stream` : `Without ${who}, from your bench`;
}

/** What the page changed about the question asked, in words (empty when nothing). */
export function adjustments(asked: { ids: number; pool: RosterPool }, used: { ids: number; pool: RosterPool }): string[] {
  const out: string[] = [];
  if (asked.ids > used.ids) out.push(`Only the first ${used.ids} chosen players are evaluated (at most ${MAX_ASSETS}, on this roster).`);
  if (asked.pool !== used.pool) out.push("A free-agent replacement is evaluated for one player at a time, so this answer uses your bench.");
  return out;
}
