import { Missing } from "@/lib/format";
import {
  RATING_CAVEAT, RATING_DESCRIPTION, RATING_LABEL, RATING_PENDING, formatRating, type Rating as RatingValue, type RatingKind,
} from "@/lib/ratings";
import { Meter } from "./Meter";
import styles from "./Rating.module.css";
import { Score } from "./Score";
import { VisuallyHidden } from "./VisuallyHidden";

/**
 * A player rating from `ratingFor()`. A 0–100 value renders as "87.4/100" with the usual ticks (`ticks`),
 * a data bar (`bar`) or plain text (`text`); a legacy-only value renders as "8.7/10 (legacy)" in muted text
 * with a tooltip and never as a bar, so a 0–10 number cannot be read as a 0–100 one. Nothing at all is the
 * muted dash. The tooltip says what the rating ranks and that it is a relative rank.
 */
export function Rating({ rating, kind, variant = "ticks", why }: {
  rating: RatingValue | null | undefined;
  kind: RatingKind;
  variant?: "ticks" | "bar" | "text";
  /** Reason shown on the dash when the rating is missing. */
  why?: string;
}) {
  const name = `${RATING_LABEL[kind]} rating`;
  if (!rating) return <Missing why={why ?? `${name} not computed on the 0–100 scale`} />;
  if (rating.scale === 10) {
    return (
      <span className={styles.legacy} title={`${RATING_PENDING} Showing the legacy 0–10 ${kind} score instead; it is not a 0–100 rating.`}>
        {formatRating(rating)}
      </span>
    );
  }
  const title = `${name}, 0–100: ${RATING_DESCRIPTION[kind]}. ${RATING_CAVEAT}`;
  if (variant === "text") {
    return (
      <span className={styles.text} title={title}>
        <span aria-hidden="true">{formatRating(rating)}</span>
        <VisuallyHidden>{`${name}: ${rating.value.toFixed(1)} out of 100`}</VisuallyHidden>
      </span>
    );
  }
  return (
    <span title={title}>
      {variant === "bar"
        ? <Meter value={rating.value} max={100} unit="/100" label={name} />
        : <Score value={rating.value} max={100} unit="/100" label={name} />}
    </span>
  );
}
