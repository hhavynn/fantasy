import { Missing } from "@/lib/format";
import styles from "./Meter.module.css";

/**
 * A number with a short bar showing where it sits in 0–`max` (a data bar for table cells). The number is
 * always printed, so the bar only speeds up scanning; `null` is the muted missing dash, never an empty bar.
 */
export function Meter({ value, max = 10, digits = 1, why = "Not computed", label, unit }: {
  value: number | null | undefined;
  max?: number;
  digits?: number;
  why?: string;
  /** Accessible name for the bar, e.g. "Opportunity score". */
  label?: string;
  /** Visible suffix after the number, e.g. "/100" for a rating. */
  unit?: string;
}) {
  if (value === null || value === undefined || Number.isNaN(value)) return <Missing why={why} />;
  const share = Math.max(0, Math.min(1, value / max));
  return (
    <span className={styles.meter}>
      <span className={styles.v}>{value.toFixed(digits)}{unit && <span className={styles.unit}>{unit}</span>}</span>
      <span className={styles.track} role="img" aria-label={`${label ? `${label}: ` : ""}${value.toFixed(digits)} ${label || unit ? "out of" : "of"} ${max}`}>
        <span className={styles.fill} style={{ width: `${share * 100}%` }} />
      </span>
    </span>
  );
}
