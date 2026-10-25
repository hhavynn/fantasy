"use client";

import { usePathname } from "next/navigation";
import { appPath, splitLeaguePath, withLeague } from "@/lib/league-scope";

/** The page path without the `/l/<lg>` league prefix: what nav highlighting and page matching compare against. */
export function useAppPathname(): string {
  return appPath(usePathname());
}

/** The league the current page is scoped to (`/l/<lg>/...`), or null on the classic site. */
export function useLeagueId(): string | null {
  return splitLeaguePath(usePathname()).league;
}

/** Builds in-app hrefs for the current league (see `withLeague`); identity on the classic site. */
export function useLeagueHref(): (href: string) => string {
  const league = useLeagueId();
  return (href: string) => withLeague(href, league);
}
