"use client";

import Link from "next/link";
import type { ComponentProps } from "react";
import { withTeam } from "@/lib/team-perspective";
import { useTeam } from "./useTeam";

/** A Link that keeps the analyzed team (`?team=`, or the last one analyzed in this tab) when it points at a
 *  team-scoped page. Usable from server components. */
export function TeamLink({ href, ...rest }: Omit<ComponentProps<typeof Link>, "href"> & { href: string }) {
  const team = useTeam();
  return <Link href={withTeam(href, team)} {...rest} />;
}
