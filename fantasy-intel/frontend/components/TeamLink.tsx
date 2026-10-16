"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import type { ComponentProps } from "react";
import { teamParamFrom, withTeam } from "@/lib/team-perspective";

/** A Link that keeps the selected team (`?team=`) when it points at another team-scoped page. */
export function TeamLink({ href, ...rest }: Omit<ComponentProps<typeof Link>, "href"> & { href: string }) {
  const team = teamParamFrom(useSearchParams());
  return <Link href={withTeam(href, team)} {...rest} />;
}
