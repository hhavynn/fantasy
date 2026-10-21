import { Badge } from "@/components/ui";
import { Missing } from "@/lib/format";
import {
  availabilityWarning, formatPoints, formatRating, rosterLabel, unrankedLabel,
  type RankingRow, type RosterTone,
} from "@/lib/rankings";
import styles from "./rankings.module.css";

/** Badge tones for the roster tones `rosterLabel` returns. */
const ROSTER_BADGE_TONE: Record<RosterTone, string> = { accent: "info", good: "good", muted: "", warn: "warn" };

/** Who has the player in this league. Text always names the state; colour only repeats it. */
export function RosterBadge({ roster }: { roster: RankingRow["roster"] }) {
  const { text, tone } = rosterLabel(roster);
  return <Badge value={text} tone={ROSTER_BADGE_TONE[tone]} title={roster.fantasy_team ?? undefined} />;
}

/** Badge values the shared `Badge` knows as availability codes (Q, D, O, IR, SSPD). */
const AVAILABILITY_BADGE: Record<string, string> = {
  QUESTIONABLE: "QUESTIONABLE", DOUBTFUL: "DOUBTFUL", OUT: "OUT", IR: "IR", SUSPENDED: "SUSPENSION",
  INJURY_RESERVE: "INJURY_RESERVE", SUSPENSION: "SUSPENSION",
};

/** Injury or bye marker for the identity line; nothing for a healthy player who plays. */
export function AvailabilityBadge({ row }: { row: Pick<RankingRow, "availability" | "game"> }) {
  const warning = availabilityWarning(row);
  if (!warning) return null;
  if (row.game.on_bye) return <Badge value="BYE" tone="warn" title="Bye week" />;
  const value = AVAILABILITY_BADGE[row.availability.status];
  return value ? <Badge value={value} /> : null;
}

/** Projected points, or a muted dash that says why there is none. */
export function Points({ value, status }: { value: number | null; status: string }) {
  if (value !== null && value !== undefined) return <>{formatPoints(value)}</>;
  return <Missing why={status === "ok" ? "No projection" : unrankedLabel(status)} />;
}

/** A 0–100.0 rating with a slim bar. The number is always printed; the bar only speeds up scanning. */
export function RatingBar({ value, label, why = "Too few games for a rating" }: { value: number | null; label: string; why?: string }) {
  if (value === null || value === undefined || Number.isNaN(value)) return <Missing why={why} />;
  const text = formatRating(value);
  return (
    <span className={styles.rating}>
      <span className={styles.ratingValue}>{text}</span>
      <span className={styles.track} role="img" aria-label={`${label}: ${text} of 100`}>
        <span className={styles.fill} style={{ width: `${Math.min(100, Math.max(0, value))}%` }} />
      </span>
    </span>
  );
}

/** A rating shown as a plain number (the secondary ratings). */
export function RatingText({ value, why = "Too few games for a rating" }: { value: number | null; why?: string }) {
  return value === null || value === undefined || Number.isNaN(value) ? <Missing why={why} /> : <>{formatRating(value)}</>;
}
