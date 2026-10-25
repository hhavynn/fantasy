import Link from "@/components/LeagueLink";
import { cx } from "@/lib/cx";
import { ScrollToActive } from "./ScrollToActive";
import styles from "./Tabs.module.css";

export type TabItem = { href: string; label: string; count?: number | null };

/**
 * URL-backed tab bar for switching views within a page via search params
 * (`href: "?view=usage"`). Plain links: no client state, back/forward and sharing just work.
 * `current` is the `href` of the active item; `label` names the tab group for assistive tech.
 */
export function Tabs({ items, current, label, className }: {
  items: readonly TabItem[];
  current: string;
  label: string;
  className?: string;
}) {
  return (
    <nav className={cx(styles.tabs, className)} aria-label={label}>
      {items.map((item) => (
        <Link
          key={item.href}
          href={item.href}
          scroll={false}
          className={styles.tab}
          aria-current={item.href === current ? "page" : undefined}
        >
          {item.label}
          {item.count !== undefined && item.count !== null ? <span className={styles.count}>{item.count}</span> : null}
        </Link>
      ))}
      <ScrollToActive current={current} />
    </nav>
  );
}
