import Link from "next/link";
import type { ReactNode } from "react";
import { cx } from "@/lib/cx";
import { PlayerPhoto } from "./PlayerPhoto";
import { Pos } from "./ui/Pos";
import styles from "./PlayerIdentity.module.css";

/**
 * A player's identity for tables, cards and lists: photo, name (linked to the profile), and one line
 * of supporting metadata (position, NFL team, then anything passed in `meta`, e.g. an injury badge).
 * The name stays the scan anchor; metadata is quieter. `size="lg"` is for cards.
 */
export function PlayerIdentity({ id, name, position, team, meta, lead, size = "md", link = true, className }: {
  id: number;
  name: string | null;
  position?: string | null;
  /** NFL team abbreviation; `null` shows "FA". Omit to leave the team out. */
  team?: string | null;
  meta?: ReactNode;
  /** A short tag before the photo, such as the lineup slot, so it stays with the name in a pinned column. */
  lead?: ReactNode;
  size?: "md" | "lg";
  link?: boolean;
  className?: string;
}) {
  const label = name ?? `Player #${id}`;
  const showTeam = team !== undefined;
  return (
    <span className={cx("player-identity", styles.identity, size === "lg" && styles.lg, className)}>
      {lead ? <span className={styles.lead}>{lead}</span> : null}
      <PlayerPhoto id={id} name={name} size={size === "lg" ? 40 : 30} className={styles.photo} />
      <span className={styles.text}>
        {link
          ? <Link className={cx("player-link", styles.name)} href={`/players?id=${id}`} title={label}>{label}</Link>
          : <span className={styles.name} title={label}>{label}</span>}
        {(position || showTeam || meta) ? (
          <span className={styles.meta}>
            {position ? <Pos value={position} /> : null}
            {showTeam ? <span className={styles.team}>{team ?? "FA"}</span> : null}
            {meta}
          </span>
        ) : null}
      </span>
    </span>
  );
}
