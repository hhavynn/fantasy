import type { Metadata } from "next";
import Link from "@/components/LeagueLink";
import { PageHeader, Section } from "@/components/ui";
import { NAV_GROUPS } from "@/lib/navigation";
import styles from "./route-state.module.css";

export const metadata: Metadata = { title: "Page not found" };

export default function NotFound() {
  return (
    <div className="stack">
      <PageHeader title="Page not found" meta="That address doesn't match a page in Fantasy Intel." />
      <Section title="Go to">
        <nav className={styles.groups} aria-label="All pages">
          {NAV_GROUPS.map((g) => (
            <div key={g.label} className={styles.group}>
              <p className={styles.groupTitle}>{g.label}</p>
              {g.pages.map((p) => <Link key={p.href} href={p.href}>{p.label}</Link>)}
            </div>
          ))}
        </nav>
      </Section>
    </div>
  );
}
