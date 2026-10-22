"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { cx } from "@/lib/cx";
import { RULES_PATH } from "@/lib/league-rules";
import { useLeagueHref } from "./useAppPathname";
import styles from "./RulesLink.module.css";

/** The part of the rules page a link opens: lineup slots, scoring, or lock, waivers and trades. */
export type RulesSection = "roster" | "scoring" | "transactions";

const LABELS: Record<RulesSection, string> = {
  roster: "League lineup slots and who can play them",
  scoring: "League scoring rules",
  transactions: "League lineup lock, waiver and trade rules",
};

/**
 * A small link to the read-only League rules page (optionally one section of it), kept in the current league on the
 * multi-league site. For decision pages that rest on the league's settings (Start/Sit, Matchup, Waivers, Trades), so a
 * manager can check the slots and scoring behind a recommendation.
 */
export function RulesLink({ section, children = "League rules", className }: {
  section?: RulesSection;
  children?: ReactNode;
  className?: string;
}) {
  const href = useLeagueHref()(section ? `${RULES_PATH}#${section}` : RULES_PATH);
  const label = section ? LABELS[section] : "League rules: slots, scoring, waivers and trades";
  return (
    <Link className={cx(styles.link, className)} href={href} title={label} aria-label={children === "League rules" ? label : undefined}>
      <svg className={styles.icon} width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <rect x="2.5" y="1.5" width="9" height="11" rx="1.5" />
        <path d="M5 5h4M5 7.5h4M5 10h2" />
      </svg>
      {children}
    </Link>
  );
}
