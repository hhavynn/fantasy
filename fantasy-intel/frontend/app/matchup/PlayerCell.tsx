import { PlayerLink } from "@/components/PlayerLink";
import { OriginTag } from "@/components/ProjectionNotice";
import { Badge, Pos } from "@/components/ui";
import { num } from "@/lib/format";
import { hasDesignation, type PlayerRow } from "@/lib/matchup";
import styles from "./matchup.module.css";

/** A player in a lineup: name, position, NFL team, bye and injury badges. `row` null = no slot on this side. */
export function PlayerCell({ row, align = "left", emptyReason = "No player is projected for this slot" }: {
  row: PlayerRow | null;
  align?: "left" | "right";
  emptyReason?: string;
}) {
  if (row === null) return <span className={styles.none} title="This team has no such slot">—</span>;
  if (row.id === null) return <span className={styles.empty} title={emptyReason}>Empty</span>;
  return (
    <span className={`${styles.player} ${align === "right" ? styles.playerRight : ""}`}>
      <PlayerLink id={row.id} name={row.name} />
      <Pos value={row.position} />
      {row.pro_team ? <span className={styles.team3}>{row.pro_team}</span> : null}
      {row.bye ? <Badge value="BYE" tone="warn" title="His NFL team has no game this week" /> : null}
      {hasDesignation(row.injury) ? <Badge value={row.injury} /> : null}
      <OriginTag origin={row.origin} ours />
    </span>
  );
}

/** A points cell value: a measured zero renders as 0.0, an unknown as the muted dash. */
export function Points({ value, why = "No ESPN projection" }: { value: number | null; why?: string }) {
  return <>{num(value, 1, why)}</>;
}
