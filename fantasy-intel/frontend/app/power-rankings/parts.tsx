import Link from "next/link";
import { Delta } from "@/components/ui";
import { Missing } from "@/lib/format";
import { barWidth, componentValueText, movementText, ordinal, type PrComponentKey, type PrTeam } from "@/lib/power-rankings";
import styles from "./power.module.css";

/** A 0-100 score bar. The number beside it is always printed, so the bar only speeds up scanning. */
export function ScoreBar({ score, label, strong = false }: { score: number | null | undefined; label: string; strong?: boolean }) {
  const w = barWidth(score);
  if (w === null) return null;
  return (
    <span className={strong ? `${styles.track} ${styles.trackStrong}` : styles.track} role="img"
      aria-label={`${label}: ${w.toFixed(0)} out of 100`}>
      <span className={styles.fill} style={{ width: `${w}%` }} />
    </span>
  );
}

/** Rank movement since last week as a signed change; a dash with the reason when there is no previous rank. */
export function Movement({ team, why }: { team: PrTeam; why: string }) {
  const text = movementText(team);
  if (text === null) return <Missing why={why} />;
  return (
    <span title={text}>
      <Delta value={team.movement} digits={0} />
    </span>
  );
}

/** One component cell: its own value, its 0-100 league score as a bar, and its rank. */
export function ComponentCell({ team, k, label, total }: { team: PrTeam; k: PrComponentKey; label: string; total: number }) {
  const c = team.components[k];
  if (c.value === null) return <Missing why={team.unranked_reason ?? "Not computed"} />;
  return (
    <span className={styles.cell} title={`${label}: score ${c.score?.toFixed(0) ?? "—"} of 100${c.rank ? `, ${ordinal(c.rank)} of ${total}` : ""}`}>
      <span className={styles.cellValue}>{componentValueText(k, c.value)}</span>
      <ScoreBar score={c.score} label={`${label} score`} />
      {c.rank !== null && <span className={styles.cellRank}>{ordinal(c.rank)}</span>}
    </span>
  );
}

export function TeamName({ team, href }: { team: PrTeam; href: string }) {
  return (
    <Link href={href} className={styles.teamName} title={team.name}>
      {team.name}
    </Link>
  );
}
