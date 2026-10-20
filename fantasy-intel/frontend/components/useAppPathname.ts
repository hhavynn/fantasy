"use client";

import { usePathname } from "next/navigation";
import { appPath, splitLeaguePath } from "@/lib/league-scope";

/** The page path without the `/l/<lg>` league prefix: what nav highlighting and page matching compare against. */
export function useAppPathname(): string {
  return appPath(usePathname());
}

/** The league the current page is scoped to (`/l/<lg>/...`), or null on the classic site. */
export function useLeagueId(): string | null {
  return splitLeaguePath(usePathname()).league;
}
