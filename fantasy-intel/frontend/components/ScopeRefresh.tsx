"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef } from "react";
import { crossesLeagueScope } from "@/lib/league-scope";

/**
 * The root layout renders the header for the league in the URL, but Next keeps that layout across client-side
 * navigations. When a link crosses the league scope (/leagues -> /l/<lg>/, a league -> /terms, one league -> another),
 * refresh once so the server renders the header again. Only a change triggers it, never the first render, so it
 * cannot loop.
 */
export function ScopeRefresh() {
  const pathname = usePathname();
  const router = useRouter();
  const previous = useRef<string | null>(null);
  useEffect(() => {
    if (crossesLeagueScope(previous.current, pathname)) router.refresh();
    previous.current = pathname;
  }, [pathname, router]);
  return null;
}
