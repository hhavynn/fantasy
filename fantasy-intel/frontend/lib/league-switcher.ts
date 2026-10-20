// The league switcher's recent-leagues list (local storage) and its labels. Pure, so node --test covers it.
import { isLeagueId } from "./league-scope.ts";

export type RecentLeague = { id: string; name: string };
export const RECENT_KEY = "fantasy-intel:leagues";
export const MAX_RECENT = 8;

/** Parse stored JSON defensively: anything malformed is dropped, never trusted. */
export function parseRecent(raw: string | null): RecentLeague[] {
  if (!raw) return [];
  try {
    const value = JSON.parse(raw);
    if (!Array.isArray(value)) return [];
    return value.filter((x): x is RecentLeague => !!x && isLeagueId(x.id) && typeof x.name === "string")
      .map((x) => ({ id: x.id, name: x.name.slice(0, 80) })).slice(0, MAX_RECENT);
  } catch {
    return [];
  }
}

/** Most recent first, no duplicates, bounded. */
export function rememberLeague(list: RecentLeague[], entry: RecentLeague): RecentLeague[] {
  if (!isLeagueId(entry.id)) return list;
  return [{ id: entry.id, name: entry.name.slice(0, 80) }, ...list.filter((x) => x.id !== entry.id)].slice(0, MAX_RECENT);
}

/** "Data as of Oct 20, 12:00" from the served version's commit time (ISO), or null. */
export function dataAsOf(committedAt: string | null | undefined, timeZone?: string): string | null {
  if (!committedAt) return null;
  const d = new Date(committedAt);
  if (Number.isNaN(d.getTime())) return null;
  return `Data as of ${d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZone })}`;
}
