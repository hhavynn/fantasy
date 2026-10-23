"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ComponentProps } from "react";
import { leagueLinkHref } from "@/lib/league-scope";

/**
 * `next/link` for in-app hrefs that stays in the page's league: on `/l/<lg>/...` a string href such as
 * `/players?id=1` renders as `/l/<lg>/players?id=1` (`leagueLinkHref`), so the click goes straight to that league's
 * page instead of through the proxy's last-viewed-league cookie redirect (another tab may have changed it). On the
 * classic site, and for external, relative (`?q`, `#x`), unscoped (`/leagues`, `/terms`) and already-scoped hrefs, it
 * is exactly `next/link`. Usable from server components, like `TeamLink`.
 */
export default function LeagueLink({ href, ...rest }: ComponentProps<typeof Link>) {
  const pathname = usePathname();
  return <Link href={typeof href === "string" ? leagueLinkHref(href, pathname) : href} {...rest} />;
}

export { LeagueLink };
