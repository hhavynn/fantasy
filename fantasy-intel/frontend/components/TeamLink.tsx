"use client";

import Link from "next/link";
import type { ComponentProps } from "react";
import { withLeague } from "@/lib/league-scope";
import { withTeam } from "@/lib/team-perspective";
import { useTeam } from "./useTeam";

/** A Link that keeps the analyzed team (`?team=`, or the last one analyzed in this tab) when it points at a
 *  team-scoped page. Usable from server components. `league` (optional) scopes the href to that league (`/l/<lg>`)
 *  after the team is added, so the link never depends on the league cookie. */
export function TeamLink({ href, league, ...rest }: Omit<ComponentProps<typeof Link>, "href"> & { href: string; league?: string | null }) {
  const team = useTeam();
  return <Link href={withLeague(withTeam(href, team), league)} {...rest} />;
}
