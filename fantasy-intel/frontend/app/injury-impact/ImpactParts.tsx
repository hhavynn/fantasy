import type { ReactNode } from "react";
import { PlayerIdentity } from "@/components/PlayerIdentity";
import { Badge, VisuallyHidden } from "@/components/ui";
import { Missing, num } from "@/lib/format";
import { rosterText, type Roster } from "@/lib/injury-impact";
import styles from "./injury-impact.module.css";

/** Marks a number's kind. Observed = counted from games; estimate = a rule or a model (dashed, never solid). */
export function Kind({ kind }: { kind: "observed" | "estimate" | "espn" }) {
  const text = kind === "observed" ? "Observed" : kind === "estimate" ? "Estimate" : "ESPN model";
  const title = kind === "observed" ? "Counted from this season's games"
    : kind === "estimate" ? "Pro-rata split estimate" : "ESPN's own weekly projection";
  return <span className={kind === "observed" ? styles.kindObserved : styles.kindEstimate} title={title}>{text}</span>;
}

/** Who has the player in the selected league. Text always names the state; colour only repeats it. */
export function RosterBadge({ roster }: { roster: Roster }) {
  const { text, tone } = rosterText(roster);
  const owned = roster.percent_owned !== null && !roster.is_mine && roster.status !== "ROSTERED"
    ? `${roster.percent_owned.toFixed(0)}% rostered across ESPN` : undefined;
  return <Badge value={text} tone={tone} title={roster.fantasy_team ?? owned} />;
}

/** The player cell: identity with photo and profile link when ESPN knows him, else his name. */
export function Who({ id, name, position, team, meta }: {
  id: number | null; name: string; position?: string | null; team?: string | null; meta?: ReactNode;
}) {
  if (id === null) {
    return <span className={styles.plainName} title="Not in this league's ESPN player pool">{name}</span>;
  }
  return <PlayerIdentity id={id} name={name} position={position} team={team} meta={meta} />;
}

/** "4.0 → 10.0" with the games behind each side (games he was available for; not playing counts as 0); a dash
 *  when a side has no game. */
export function WithWithout({ withV, withoutV, withGames, withoutGames, digits = 1 }: {
  withV: number | null | undefined; withoutV: number | null | undefined;
  withGames: number | null | undefined; withoutGames: number | null | undefined; digits?: number;
}) {
  if (withoutGames === null || withoutGames === undefined || withoutV === null || withoutV === undefined) {
    return <Missing why="No team game without him while this player was on the roster and available" />;
  }
  return (
    <span className={styles.split}>
      <span>{num(withV ?? null, digits, "Not on the roster, or unavailable, in the games with him")}</span>
      <span aria-hidden="true" className={styles.arrow}>→</span>
      <VisuallyHidden> without him: </VisuallyHidden>
      <strong>{num(withoutV, digits)}</strong>
      <span className={styles.games}>{withGames ?? 0}/{withoutGames} g</span>
    </span>
  );
}

/** A share 0–1 as a dashed estimate bar plus the number. */
export function ShareBar({ share, extra, unit }: { share: number | null; extra: number | null; unit: string }) {
  if (share === null) return <Missing why="No teammate in the pool had volume to split by" />;
  return (
    <span className={styles.share} title={`${(share * 100).toFixed(0)}% of the vacated ${unit} if split pro rata`}>
      <span className={styles.shareTrack} aria-hidden="true">
        <span className={styles.shareFill} style={{ width: `${Math.max(0, Math.min(1, share)) * 100}%` }} />
      </span>
      <span className={styles.shareValue}>{(share * 100).toFixed(0)}%</span>
      <span className={styles.shareExtra}>{extra === null ? "" : `+${extra.toFixed(1)}`}</span>
    </span>
  );
}

/** Signed per-game change with a glyph, so the direction never depends on colour. */
export function Change({ v, digits = 1, why }: { v: number | null; digits?: number; why?: string }) {
  if (v === null) return <Missing why={why ?? "No observed split"} />;
  const glyph = v > 0 ? "▲" : v < 0 ? "▼" : "";
  return (
    <span className={v > 0 ? "up" : v < 0 ? "down" : undefined}>
      {glyph && <span aria-hidden="true" className={styles.glyph}>{glyph}</span>}
      {v > 0 ? "+" : ""}{v.toFixed(digits)}
    </span>
  );
}
