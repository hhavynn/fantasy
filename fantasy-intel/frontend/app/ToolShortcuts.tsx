import { TeamLink } from "@/components/TeamLink";
import styles from "./home.module.css";

const TOOLS = [
  { href: "/start-sit", title: "Start/Sit", text: "Submitted lineup against the best projected one",
    icon: <><path d="M4 9.5 7.5 13 14 5.5" /></> },
  { href: "/waivers", title: "Waivers", text: "Pickups for this roster, with the drop",
    icon: <><path d="M9 3.5v11M3.5 9h11" /></> },
  { href: "/trades", title: "Trade analyzer", text: "Lineup impact of any trade, week by week",
    icon: <><path d="M3 6h11l-3-3" /><path d="M15 12H4l3 3" /></> },
  { href: "/rankings", title: "Player rankings", text: "Every player by projection, role and usage",
    icon: <><circle cx="8" cy="8" r="4.5" /><path d="m11.5 11.5 3.5 3.5" /></> },
] as const;

/** Where to go next: the four workspaces most weeks start from. Copy describes the tool, never a recommendation. */
export function ToolShortcuts() {
  return (
    <nav aria-label="Tools" className={styles.toolsNav}>
      <p className={styles.toolsLabel}>Jump to</p>
      <ul className={styles.tools}>
        {TOOLS.map((t) => (
          <li key={t.href}>
            <TeamLink className={styles.tool} href={t.href}>
              <span className={styles.toolIcon}>
                <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{t.icon}</svg>
              </span>
              <span className={styles.toolTitle}>{t.title}</span>
              <span className={styles.toolText}>{t.text}</span>
            </TeamLink>
          </li>
        ))}
      </ul>
    </nav>
  );
}
