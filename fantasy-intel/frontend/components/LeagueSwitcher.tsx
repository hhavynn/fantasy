"use client";

import Link from "next/link";
import { useEffect, useSyncExternalStore } from "react";
import { leaguePath } from "@/lib/league-scope";
import { RECENT_KEY, dataAsOf, parseRecent, rememberLeague, type RecentLeague } from "@/lib/league-switcher";
import type { ScopedLeague } from "@/lib/league-scope.server";
import styles from "./LeagueSwitcher.module.css";

// Recently viewed leagues live in localStorage (per browser); storage can be blocked, then the menu lists only the
// current league's escape links.
const listeners = new Set<() => void>();
const subscribe = (cb: () => void) => {
  listeners.add(cb);
  return () => { listeners.delete(cb); };
};
let cachedRaw: string | null = null;
let cached: RecentLeague[] = [];
function readRecent(): RecentLeague[] {
  try {
    const raw = window.localStorage.getItem(RECENT_KEY);
    if (raw !== cachedRaw) {            // a stable snapshot while storage is unchanged
      cachedRaw = raw;
      cached = parseRecent(raw);
    }
    return cached;
  } catch {
    return cached;
  }
}
const EMPTY: RecentLeague[] = [];

/** Which league the site is showing, when its data is from, and the leagues recently viewed in this browser. */
export function LeagueSwitcher({ current }: { current: ScopedLeague }) {
  const recent = useSyncExternalStore(subscribe, readRecent, () => EMPTY);
  useEffect(() => {
    try {
      const next = rememberLeague(parseRecent(window.localStorage.getItem(RECENT_KEY)), { id: current.id, name: current.name });
      window.localStorage.setItem(RECENT_KEY, JSON.stringify(next));
    } catch {
      return;
    }
    listeners.forEach((l) => l());
  }, [current.id, current.name]);
  const asOf = dataAsOf(current.committedAt);
  return (
    <details className={styles.switcher}>
      <summary className={styles.summary} title={asOf ?? undefined}>
        <span className={styles.name}>{current.name}</span>
        {asOf ? <span className={styles.asof}>{asOf}</span> : null}
      </summary>
      <div className={styles.menu} role="menu">
        {recent.filter((l) => l.id !== current.id).map((l) => (
          <Link key={l.id} role="menuitem" href={leaguePath(l.id, "/")} className={styles.item}>{l.name}</Link>
        ))}
        <Link role="menuitem" href="/leagues" className={styles.item}>Leagues…</Link>
        {/* prefetch off: following this link clears the league cookie, which a prefetch must not do */}
        <Link role="menuitem" href="/?league=clear" prefetch={false} className={styles.item}>Classic site</Link>
      </div>
    </details>
  );
}
