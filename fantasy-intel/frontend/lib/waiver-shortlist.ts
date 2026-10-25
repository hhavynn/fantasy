import { isLeagueId } from "./league-scope.ts";

/** No shared fallback key: the classic/unscoped site cannot persist a shortlist. */
export function shortlistKey(leagueId: string | null): string | null {
  return isLeagueId(leagueId) ? `fi:waiver-shortlist:v1:${leagueId}` : null;
}

/**
 * Whether a player can be saved to the waiver shortlist: only one the provider lists as a free agent or on waivers.
 * A rostered player, and one whose availability is unknown (a null pool status), is not offered.
 */
export function isShortlistCandidate(availability: string | null | undefined, fantasyTeam?: string | null): boolean {
  return !fantasyTeam && (availability === "FREEAGENT" || availability === "WAIVERS");
}

const validId = (id: unknown): id is number => typeof id === "number" && Number.isSafeInteger(id) && id !== 0;

export function parseShortlist(raw: string | null): number[] {
  try {
    const value: unknown = JSON.parse(raw ?? "[]");
    return Array.isArray(value) && value.every(validId) ? [...new Set<number>(value)] : [];
  } catch {
    return [];
  }
}

export function toggleShortlist(ids: readonly number[], id: number): number[] {
  if (!validId(id)) return [...ids];
  return ids.includes(id) ? ids.filter((saved) => saved !== id) : [...ids, id];
}

export function readShortlist(storage: Pick<Storage, "getItem">, leagueId: string | null): { ids: number[]; available: boolean } {
  const key = shortlistKey(leagueId);
  if (!key) return { ids: [], available: false };
  try {
    return { ids: parseShortlist(storage.getItem(key)), available: true };
  } catch {
    return { ids: [], available: false };
  }
}

export function writeShortlist(storage: Pick<Storage, "setItem">, leagueId: string | null, ids: number[]): boolean {
  const key = shortlistKey(leagueId);
  if (!key) return false;
  try {
    storage.setItem(key, JSON.stringify(ids));
    return true;
  } catch {
    return false;
  }
}
