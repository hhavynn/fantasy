"use client";

import Link from "@/components/LeagueLink";
import { useNavGroups } from "@/components/NavVisibility";
import styles from "./route-state.module.css";

/** Every page this visitor may open, grouped like the nav: without the owner research pages when the nav hides them. */
export default function NotFoundLinks() {
  const groups = useNavGroups();
  return (
    <nav className={styles.groups} aria-label="All pages">
      {groups.map((g) => (
        <div key={g.label} className={styles.group}>
          <p className={styles.groupTitle}>{g.label}</p>
          {g.pages.map((p) => <Link key={p.href} href={p.href}>{p.label}</Link>)}
        </div>
      ))}
    </nav>
  );
}
