"use client";

import { useRouter } from "next/navigation";
import { useTransition } from "react";
import styles from "./TeamPerspective.module.css";
import { useLeagueHref } from "./useAppPathname";

/** The team picker: each option's value is the page URL for that team, so a change is a navigation (back works). On a
 *  league page it stays in that league (`/l/<lg>/...`) instead of following the last-viewed-league cookie. A league URL's
 *  `?league=clear` never reaches the options: proxy.ts drops it from the rewritten page's query (lib/league-scope.ts). */
export function TeamPerspectiveSelect({ options, placeholder }: {
  options: { value: string; label: string; selected: boolean }[];
  placeholder: string | null;
}) {
  const router = useRouter();
  const leagueHref = useLeagueHref();
  const [pending, start] = useTransition();
  const selected = options.find((o) => o.selected)?.value ?? "";
  return (
    <label className={styles.field}>
      <span className={styles.label}>Analyzing</span>
      <select className={styles.select} value={selected} aria-busy={pending || undefined}
              onChange={(e) => start(() => router.push(leagueHref(e.target.value)))}>
        {placeholder && <option value="" disabled>{placeholder}</option>}
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </label>
  );
}
