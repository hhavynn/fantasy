"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ComponentProps } from "react";
import { leagueLinkHref } from "@/lib/league-scope";
import { withTeam } from "@/lib/team-perspective";
import { useTeam } from "./useTeam";

/** A Link that keeps the analyzed team (`?team=`, or the last one analyzed in this tab) when it points at a
 *  team-scoped page. Usable from server components. The href is scoped to `league` when given, else to the league the
 *  page is in (`/l/<lg>`), after the team is added, so the link never depends on the league cookie. */
export function TeamLink({ href, league, ...rest }: Omit<ComponentProps<typeof Link>, "href"> & { href: string; league?: string | null }) {
  const team = useTeam();
  const pathname = usePathname();
  return <Link href={leagueLinkHref(withTeam(href, team), pathname, league)} {...rest} />;
}
