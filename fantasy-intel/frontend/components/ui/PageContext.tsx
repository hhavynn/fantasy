"use client";

import { pageContext, pageContextLabel } from "@/lib/navigation";
import { useAppPathname } from "../useAppPathname";

/**
 * The page's place in the nav registry ("Players · Research", "Trades") for a `PageHeader` that names no eyebrow of
 * its own, so every page header opens with the same context line. Nothing for a route outside the five groups.
 */
export function PageContext({ className }: { className?: string }) {
  const label = pageContextLabel(pageContext(useAppPathname()));
  return label ? <p className={className}>{label}</p> : null;
}
