// Pure state logic for the interactive trade builder (no React, no fetch), so it can be unit-tested
// with `node --test` (see lib/trade-builder.test.ts).

export type BuilderPlayer = { espn_player_id: number; position: string | null; on_ir: boolean };
export type BuilderTeam = { team_id: number; roster: BuilderPlayer[] };

export type Selection = {
  ta: number | null;
  tb: number | null;
  a: number[];          // Team A sends
  b: number[];          // Team B sends
  sw: number | null;    // explicit start week (null = earliest actionable)
};

function roster(teams: BuilderTeam[], id: number | null): BuilderPlayer[] {
  return teams.find((t) => t.team_id === id)?.roster ?? [];
}

function keepOn(ids: number[], players: BuilderPlayer[]): number[] {
  const own = new Set(players.map((p) => p.espn_player_id));
  return ids.filter((id) => own.has(id));
}

/** Initial state from URL values: unknown teams fall back, selections not on the chosen roster are dropped. */
export function initialSelection(teams: BuilderTeam[], raw: { ta?: number | null; tb?: number | null; a?: number[];
  b?: number[]; sw?: number | null }, mine?: number | null): Selection {
  const has = (id: number | null | undefined) => id !== null && id !== undefined && teams.some((t) => t.team_id === id);
  const ta = has(raw.ta) ? raw.ta! : has(mine) ? mine! : teams[0]?.team_id ?? null;
  const tb = has(raw.tb) ? raw.tb! : teams.find((t) => t.team_id !== ta)?.team_id ?? null;
  return {
    ta, tb,
    a: unique(keepOn(raw.a ?? [], roster(teams, ta))),
    b: unique(keepOn(raw.b ?? [], roster(teams, tb))),
    sw: raw.sw ?? null,
  };
}

function unique(ids: number[]): number[] {
  return [...new Set(ids)];
}

/** Change one side's team. That side's picks that are not on the new roster are cleared; the other side is kept. */
export function changeTeam(s: Selection, teams: BuilderTeam[], side: "a" | "b", team: number): Selection {
  if (side === "a") return { ...s, ta: team, a: keepOn(s.a, roster(teams, team)), b: keepOn(s.b, roster(teams, s.tb)) };
  return { ...s, tb: team, b: keepOn(s.b, roster(teams, team)), a: keepOn(s.a, roster(teams, s.ta)) };
}

/**
 * Toggle one player on a side. With `maxPerSide`, adding a player to a side that is already full is a
 * no-op (removing always works), so the picker can never build a selection the analyzer would reject.
 */
export function togglePlayer(s: Selection, side: "a" | "b", id: number, maxPerSide?: number): Selection {
  const cur = side === "a" ? s.a : s.b;
  if (!cur.includes(id) && maxPerSide !== undefined && cur.length >= maxPerSide) return s;
  const next = cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id];
  return side === "a" ? { ...s, a: next } : { ...s, b: next };
}

export function clearPicks(s: Selection): Selection {
  return { ...s, a: [], b: [] };
}

export type SideLimit = { count: number; max: number; remaining: number; atLimit: boolean; over: boolean };

/** How full one side is against the analyzer's per-side limit. */
export function sideLimit(count: number, max: number): SideLimit {
  return { count, max, remaining: Math.max(0, max - count), atLimit: count >= max, over: count > max };
}

/** Display order of positions in the roster picker; anything else follows alphabetically, unknown last. */
export const POSITION_ORDER = ["QB", "RB", "WR", "TE", "K", "D/ST"];

function positionKey(position: string | null): [number, string] {
  if (position === null) return [POSITION_ORDER.length + 1, ""];
  const i = POSITION_ORDER.indexOf(position);
  return i >= 0 ? [i, position] : [POSITION_ORDER.length, position];
}

export function comparePositions(a: string | null, b: string | null): number {
  const [ai, an] = positionKey(a);
  const [bi, bn] = positionKey(b);
  return ai - bi || an.localeCompare(bn);
}

export type RosterGroup<T> = { position: string; players: T[] };

/** Groups players by position in picker order; players keep their input order within a group. */
export function groupByPosition<T extends { position: string | null }>(players: readonly T[]): RosterGroup<T>[] {
  const groups = new Map<string, T[]>();
  for (const p of players) {
    const key = p.position ?? "?";
    groups.set(key, [...(groups.get(key) ?? []), p]);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => comparePositions(a === "?" ? null : a, b === "?" ? null : b))
    .map(([position, ps]) => ({ position, players: ps }));
}

/** Positions present in a roster, in picker order (the filter chips). */
export function positionsIn(players: readonly { position: string | null }[]): string[] {
  return groupByPosition(players).map((g) => g.position);
}

/** Roster search: `query` matches name or NFL team (case-insensitive, whitespace-trimmed); `position` "" means all. */
export function filterRoster<T extends { name?: string | null; position: string | null; pro_team?: string | null }>(
  players: readonly T[], query: string, position: string,
): T[] {
  const q = query.trim().toLowerCase();
  return players.filter((p) => {
    if (position !== "" && (p.position ?? "?") !== position) return false;
    if (q === "") return true;
    return (p.name ?? "").toLowerCase().includes(q) || (p.pro_team ?? "").toLowerCase().includes(q);
  });
}

export function setStartWeek(s: Selection, sw: number | null): Selection {
  return { ...s, sw };
}

export function shapeLabel(s: Selection): string {
  return `${s.a.length}-for-${s.b.length}`;
}

export type Problem = { code: "same_team" | "missing_team" | "empty_side" | "too_many"; text: string };

export function problems(s: Selection, maxPerSide: number): Problem[] {
  const out: Problem[] = [];
  if (s.ta === null || s.tb === null) out.push({ code: "missing_team", text: "Choose both teams." });
  else if (s.ta === s.tb) out.push({ code: "same_team", text: "Team A and Team B are the same team. Choose two different teams." });
  if (s.a.length === 0 || s.b.length === 0) out.push({ code: "empty_side", text: "Pick at least one player on each side." });
  if (s.a.length > maxPerSide || s.b.length > maxPerSide) {
    out.push({ code: "too_many", text: `Each side can send at most ${maxPerSide} players (the analyzer's per-side limit).` });
  }
  return out;
}

export function canEvaluate(s: Selection, maxPerSide: number): boolean {
  return problems(s, maxPerSide).length === 0;
}

/** Stable identity of a selection: the evaluated result is shown only while it equals the current selection. */
export function selectionKey(s: Selection): string {
  const ids = (x: number[]) => [...x].sort((p, q) => p - q).join(",");
  return `${s.ta}|${s.tb}|${ids(s.a)}|${ids(s.b)}|${s.sw ?? ""}`;
}

export function toQuery(s: Selection): string {
  const q = new URLSearchParams();
  if (s.ta !== null) q.set("ta", String(s.ta));
  if (s.tb !== null) q.set("tb", String(s.tb));
  s.a.forEach((id) => q.append("a", String(id)));
  s.b.forEach((id) => q.append("b", String(id)));
  if (s.sw !== null) q.set("sw", String(s.sw));
  return q.toString();
}

export type RosterPreview = { activeAfter: number; capacity: number; overCapacityBy: number;
  positionsOver: { position: string; count: number; limit: number }[] };

/** What the trade does to one roster before forced drops (the evaluator picks the drops exactly). */
export function rosterPreview(own: BuilderPlayer[], sends: number[], gets: BuilderPlayer[], capacity: number,
  limits: Record<string, number>): RosterPreview {
  const out = new Set(sends);
  const before = own.filter((p) => !p.on_ir).length;
  const after = own.filter((p) => !out.has(p.espn_player_id));
  const activeAfter = after.filter((p) => !p.on_ir).length + gets.length;   // incoming players arrive active
  const count = (ps: BuilderPlayer[], pos: string) => ps.filter((p) => p.position === pos).length;
  const positionsOver = Object.entries(limits).flatMap(([pos, lim]) => {
    const c = count(after, pos) + count(gets, pos);
    const allowed = Math.max(lim, count(own, pos));
    return c > allowed ? [{ position: pos, count: c, limit: lim }] : [];
  });
  return { activeAfter, capacity, overCapacityBy: Math.max(0, activeAfter - Math.max(capacity, before)), positionsOver };
}
