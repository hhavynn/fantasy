// League transactions feed (GET /api/transactions): types, URL-backed filters and the display helpers the page uses.
// Every filter lives in the page URL, so a filtered view can be bookmarked and shared. The team filter is
// `manager`, never `team`: this is a league-wide page and `team` is the analyzed-team perspective (PRODUCT.md).

import type { SortDir } from "./sort.ts";

export const KINDS = ["trade", "waiver", "free_agent", "drop", "commissioner"] as const;
export type TxKind = (typeof KINDS)[number];
export const POSITIONS = ["QB", "RB", "WR", "TE", "K", "D/ST"] as const;
export type TxPosition = (typeof POSITIONS)[number];
export const SORTS = ["date", "week", "type"] as const;
export type TxSort = (typeof SORTS)[number];
export const PAGE_SIZE = 50;

export const KIND_LABEL: Record<TxKind, string> = {
  trade: "Trade", waiver: "Waiver claim", free_agent: "Free agent", drop: "Drop", commissioner: "Commissioner",
};
/** Plural chip and summary labels. */
export const KIND_PLURAL: Record<TxKind, [string, string]> = {
  trade: ["trade", "trades"], waiver: ["waiver claim", "waiver claims"], free_agent: ["free-agent move", "free-agent moves"],
  drop: ["drop", "drops"], commissioner: ["commissioner move", "commissioner moves"],
};
export const KIND_TONE: Record<TxKind, string> = { trade: "info", waiver: "good", free_agent: "good", drop: "bad", commissioner: "warn" };

export type TxTeam = { team_id: number; name: string | null; abbrev: string | null };
export type TxAfter = { weeks: number; first_week: number; last_week: number; on_roster: number; started: number; starter_points: number | null };
export type TxLeg = {
  action: "add" | "drop" | "trade";
  espn_player_id: number | null;
  full_name: string | null;
  position: string | null;
  pro_team: string | null;
  from_team: TxTeam | null;
  to_team: TxTeam | null;
  after: TxAfter | null;
};
export type TxEvent = {
  event_id: string;
  kind: TxKind;
  week: number | null;
  processed_at: string | null;
  proposed_at: string | null;
  team_ids: number[];
  teams: (TxTeam | null)[];
  bid_amount: number | null;
  legs: TxLeg[];
  unlisted_assets: string[];
  sources: { transactions: string[]; activity: string[] };
};
export type TransactionsFeed = {
  league: { league_id: number; season: number; name: string | null; current_week: number | null; is_faab: boolean | null };
  options: { managers: TxTeam[]; types: TxKind[]; positions: string[]; weeks: number[] };
  summary: { total: number; matching: number; by_type: Partial<Record<TxKind, number>>; undated: number; unplaced_week: number };
  page: { limit: number; offset: number; returned: number; total: number; next_cursor: string | null; has_prev: boolean; prev_cursor: string | null };
  events: TxEvent[];
  notes: Record<"scope" | "trades" | "dates" | "bids" | "after", string>;
};

export type TxQuery = {
  manager: number | null;
  type: TxKind | null;
  pos: TxPosition | null;
  week: number | null;
  sort: TxSort;
  dir: SortDir;
  cursor: string | null;
};

type SP = Record<string, string | string[] | undefined>;
const first = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);
const oneOf = <T extends string>(v: string | undefined, allowed: readonly T[]): T | null =>
  v !== undefined && (allowed as readonly string[]).includes(v) ? (v as T) : null;
const posInt = (v: string | undefined, max: number): number | null => {
  if (v === undefined || !/^\d{1,6}$/.test(v.trim())) return null;
  const n = Number(v.trim());
  return n >= 0 && n <= max ? n : null;
};

/** The page query from the URL. Unknown values fall back to "no filter" rather than an error, so an old bookmark
 *  still opens. Sort defaults to newest first. */
export function parseTxQuery(sp: SP): TxQuery {
  const sort = oneOf(first(sp.sort), SORTS) ?? "date";
  const dirRaw = first(sp.dir);
  const dir: SortDir = dirRaw === "asc" || dirRaw === "desc" ? dirRaw : sort === "date" ? "desc" : "asc";
  const week = posInt(first(sp.week), 30);
  const cursor = first(sp.cursor)?.trim();
  return {
    manager: posInt(first(sp.manager), 999_999),
    type: oneOf(first(sp.type), KINDS),
    pos: oneOf(first(sp.pos), POSITIONS),
    week: week !== null && week >= 1 ? week : null,
    sort,
    dir,
    cursor: cursor && cursor.length <= 200 ? cursor : null,
  };
}

/** The backend path for a query. */
export function backendPath(q: TxQuery, limit = PAGE_SIZE): string {
  const p = new URLSearchParams();
  if (q.manager !== null) p.set("manager", String(q.manager));
  if (q.type) p.set("type", q.type);
  if (q.pos) p.set("position", q.pos);
  if (q.week !== null) p.set("week", String(q.week));
  if (q.sort !== "date" || q.dir !== "desc") { p.set("sort", q.sort); p.set("dir", q.dir); }
  if (q.cursor) p.set("cursor", q.cursor);
  p.set("limit", String(limit));
  return `/api/transactions?${p}`;
}

/** Page link for `q` with `patch` applied. Changing a filter or the sort starts again from the first page; defaults
 *  are left out so links stay short. */
export function txHref(q: TxQuery, patch: Partial<TxQuery> = {}): string {
  const filterChanged = (["manager", "type", "pos", "week", "sort", "dir"] as const).some((k) => k in patch && patch[k] !== q[k]);
  const n: TxQuery = { ...q, ...patch, cursor: "cursor" in patch ? patch.cursor ?? null : filterChanged ? null : q.cursor };
  const p = new URLSearchParams();
  if (n.manager !== null) p.set("manager", String(n.manager));
  if (n.type) p.set("type", n.type);
  if (n.pos) p.set("pos", n.pos);
  if (n.week !== null) p.set("week", String(n.week));
  if (n.sort !== "date" || n.dir !== "desc") { p.set("sort", n.sort); p.set("dir", n.dir); }
  if (n.cursor) p.set("cursor", n.cursor);
  const s = p.toString();
  return s ? `/transactions?${s}` : "/transactions";
}

/** The search params SortableTh should carry: every filter, but no cursor (a new order starts at the top). */
export function sortParams(q: TxQuery): SP {
  const out: SP = {};
  if (q.manager !== null) out.manager = String(q.manager);
  if (q.type) out.type = q.type;
  if (q.pos) out.pos = q.pos;
  if (q.week !== null) out.week = String(q.week);
  return out;
}

export function hasFilters(q: TxQuery): boolean {
  return q.manager !== null || q.type !== null || q.pos !== null || q.week !== null;
}

/** "Team 3 · Trades · RB · Week 5", or "All moves" — the phone filter-box summary. */
export function filterSummary(q: TxQuery, managers: readonly TxTeam[]): string {
  const parts: string[] = [];
  if (q.manager !== null) parts.push(managers.find((m) => m.team_id === q.manager)?.name ?? `Team ${q.manager}`);
  if (q.type) parts.push(KIND_LABEL[q.type]);
  if (q.pos) parts.push(q.pos);
  if (q.week !== null) parts.push(`Week ${q.week}`);
  return parts.length ? parts.join(" · ") : "All moves";
}

/** "3 trades, 12 waiver claims and 1 drop" from the by-type counts, in the feed's type order. */
export function countsText(byType: Partial<Record<TxKind, number>>): string {
  const parts = KINDS.filter((k) => (byType[k] ?? 0) > 0).map((k) => {
    const n = byType[k] ?? 0;
    return `${n} ${KIND_PLURAL[k][n === 1 ? 0 : 1]}`;
  });
  if (parts.length <= 1) return parts[0] ?? "no moves";
  return `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
}

/** A UTC stamp from the backend in `timeZone` (the viewer's when omitted), always naming the zone:
 *  "Oct 5, 2:05 PM EDT". `locale` undefined = the runtime's (the viewer's browser on the client). */
export function formatStamp(iso: string | null | undefined, timeZone?: string, locale?: string): string | null {
  if (!iso) return null;
  const d = new Date(iso.endsWith("Z") || /[+-]\d{2}:?\d{2}$/.test(iso) ? iso : `${iso}Z`);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleString(locale, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone, timeZoneName: "short" });
}

/** "On the roster 3 of 4 weeks since (wk 5–8), started 2: 31.4 pts" for an incoming player, or null when the
 *  backend measured nothing. */
export function afterText(a: TxAfter | null | undefined): string | null {
  if (!a || a.weeks <= 0) return null;
  const span = a.first_week === a.last_week ? `wk ${a.first_week}` : `wk ${a.first_week}–${a.last_week}`;
  const base = `Rostered ${a.on_roster} of ${a.weeks} stored ${a.weeks === 1 ? "week" : "weeks"} since (${span})`;
  if (a.on_roster === 0) return base;
  const started = `started ${a.started}`;
  return a.starter_points === null || a.started === 0
    ? `${base}, ${started}`
    : `${base}, ${started}: ${a.starter_points.toFixed(1)} pts`;
}

/** What a leg did, in words: "Added", "Dropped", "To Team 4". */
export function legVerb(leg: TxLeg): string {
  if (leg.action === "add") return "Added";
  if (leg.action === "drop") return "Dropped";
  return leg.to_team?.name ? `To ${leg.to_team.name}` : "Traded";
}

/** The trade legs grouped by receiving team, in team order, then the trade's adds and drops. */
export function tradeSides(e: TxEvent): { team: TxTeam | null; legs: TxLeg[] }[] {
  const sides = new Map<number, { team: TxTeam | null; legs: TxLeg[] }>();
  const rest: TxLeg[] = [];
  for (const leg of e.legs) {
    if (leg.action === "trade" && leg.to_team) {
      const s = sides.get(leg.to_team.team_id) ?? { team: leg.to_team, legs: [] };
      s.legs.push(leg);
      sides.set(leg.to_team.team_id, s);
    } else rest.push(leg);
  }
  const out = [...sides.entries()].sort((a, b) => a[0] - b[0]).map(([, s]) => s);
  return rest.length ? [...out, { team: null, legs: rest }] : out;
}

/** The manager page for a team (league-wide, so it keeps team context only through TeamLink). */
export function managerHref(teamId: number): string {
  return `/managers/${teamId}`;
}
