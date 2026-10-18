"use client";

import { usePathname, useSearchParams } from "next/navigation";
import { useEffect, useSyncExternalStore } from "react";
import { isTeamScoped, navTeam, parseStoredTeam, teamParamFrom } from "@/lib/team-perspective";

// The last team analyzed on a team-scoped page, for this browser tab only (sessionStorage): a new tab or a shared
// link starts from the URL alone. Storage can be blocked; links then carry only the team in the current URL.
const KEY = "fantasy-intel:team";
const listeners = new Set<() => void>();
const subscribe = (cb: () => void) => {
  listeners.add(cb);
  return () => { listeners.delete(cb); };
};
function read(): string | null {
  try {
    return parseStoredTeam(window.sessionStorage.getItem(KEY));
  } catch {
    return null;
  }
}
function write(team: string | null) {
  if (read() === team) return;
  try {
    if (team === null) window.sessionStorage.removeItem(KEY);
    else window.sessionStorage.setItem(KEY, team);
  } catch {
    return;
  }
  listeners.forEach((l) => l());
}

/**
 * The analyzed team that links from this page should keep (`withTeam`): the URL's `?team=` on a team-scoped page,
 * else the team last analyzed in this tab. Visiting a team-scoped page records its selection, including the
 * default (no `team`), so "Back to default" sticks.
 */
export function useTeam(): string | null {
  const pathname = usePathname();
  const urlTeam = teamParamFrom(useSearchParams());
  const scoped = isTeamScoped(pathname);
  const last = useSyncExternalStore(subscribe, read, () => null);
  useEffect(() => {
    if (scoped) write(urlTeam);
  }, [scoped, urlTeam]);
  return navTeam(pathname, urlTeam, last);
}
