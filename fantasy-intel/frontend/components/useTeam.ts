"use client";

import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useSyncExternalStore } from "react";
import { isTeamScoped, navTeam, parseStoredTeam, teamParamFrom } from "@/lib/team-perspective";
import { useAppPathname, useLeagueId } from "./useAppPathname";

// The last team analyzed on a team-scoped page, for this browser tab only (sessionStorage): a new tab or a shared
// link starts from the URL alone. Storage can be blocked; links then carry only the team in the current URL.
// Per league: team 2 of one league is not team 2 of another. The classic (unscoped) site keeps the original key.
const BASE_KEY = "fantasy-intel:team";
const listeners = new Set<() => void>();
const subscribe = (cb: () => void) => {
  listeners.add(cb);
  return () => { listeners.delete(cb); };
};

export function teamStorageKey(league: string | null): string {
  return league ? `${BASE_KEY}:${league}` : BASE_KEY;
}

function read(key: string): string | null {
  try {
    return parseStoredTeam(window.sessionStorage.getItem(key));
  } catch {
    return null;
  }
}

function write(key: string, team: string | null) {
  if (read(key) === team) return;
  try {
    if (team === null) window.sessionStorage.removeItem(key);
    else window.sessionStorage.setItem(key, team);
  } catch {
    return;
  }
  listeners.forEach((l) => l());
}

/**
 * The analyzed team that links from this page should keep (`withTeam`): the URL's `?team=` on a team-scoped page,
 * else the team last analyzed in this tab (in this league). Visiting a team-scoped page records its selection,
 * including the default (no `team`), so "Back to default" sticks.
 */
export function useTeam(): string | null {
  const key = teamStorageKey(useLeagueId());
  const pathname = useAppPathname();
  const urlTeam = teamParamFrom(useSearchParams());
  const scoped = isTeamScoped(pathname);
  const getSnapshot = useCallback(() => read(key), [key]);
  const last = useSyncExternalStore(subscribe, getSnapshot, () => null);
  useEffect(() => {
    if (scoped) write(key, urlTeam);
  }, [key, scoped, urlTeam]);
  return navTeam(pathname, urlTeam, last);
}
