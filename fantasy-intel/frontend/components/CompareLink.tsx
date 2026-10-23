import Link from "@/components/LeagueLink";
import type { ReactNode } from "react";
import { cx } from "@/lib/cx";
import { compareHref, type IdLike } from "@/lib/compare";
import styles from "./CompareLink.module.css";

/**
 * A small link into the Player Comparison Lab. With two ids it opens the pair, with one it opens the lab with
 * that player picked and the second side empty. Pass names so the link has a specific accessible name.
 */
export function CompareLink({ a, b, aName, bName, children = "Compare", className }: {
  a?: IdLike;
  b?: IdLike;
  aName?: string | null;
  bName?: string | null;
  children?: ReactNode;
  className?: string;
}) {
  const hasPair = a !== null && a !== undefined && b !== null && b !== undefined;
  const label = hasPair
    ? `Compare ${aName ?? `player ${a}`} with ${bName ?? `player ${b}`}`
    : `Compare ${aName ?? "this player"} with another player`;
  return (
    <Link className={cx(styles.link, className)} href={compareHref(a, b)} aria-label={label} title={label}>
      <svg className={styles.icon} width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M2.5 4.5h8l-2-2" />
        <path d="M11.5 9.5h-8l2 2" />
      </svg>
      {children}
    </Link>
  );
}
