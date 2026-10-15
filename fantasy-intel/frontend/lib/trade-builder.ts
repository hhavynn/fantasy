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

export function togglePlayer(s: Selection, side: "a" | "b", id: number): Selection {
  const cur = side === "a" ? s.a : s.b;
  const next = cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id];
  return side === "a" ? { ...s, a: next } : { ...s, b: next };
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
