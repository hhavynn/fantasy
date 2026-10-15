import { Missing } from "@/lib/format";
import { cx } from "@/lib/cx";
import styles from "./Delta.module.css";

/**
 * A signed change with a direction glyph, tone and text: "▲ +1.2". Positive is good by default;
 * pass `invert` where lower is better (points against, rank). `null`/`undefined` renders as the
 * muted missing dash; a value that rounds to zero is a measured zero ("▬ 0.0"), not a change.
 */
export function Delta({ value, digits = 1, suffix = "", invert = false, className }: {
  value: number | null | undefined;
  digits?: number;
  suffix?: string;
  invert?: boolean;
  className?: string;
}) {
  if (value === null || value === undefined || Number.isNaN(value)) return <Missing />;
  const text = Math.abs(value).toFixed(digits);
  const flat = Number(text) === 0;
  const up = value > 0;
  const good = flat ? null : up !== invert;
  const tone = good === null ? styles.flat : good ? styles.good : styles.bad;
  return (
    <span className={cx(styles.delta, tone, className)}>
      <span className={styles.glyph} aria-hidden="true">{flat ? "▬" : up ? "▲" : "▼"}</span>
      <span>{flat ? "" : up ? "+" : "−"}{text}{suffix}</span>
    </span>
  );
}
