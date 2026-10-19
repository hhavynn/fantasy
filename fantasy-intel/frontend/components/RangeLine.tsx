import { VisuallyHidden } from "@/components/ui";
import { RANGE_TITLE, rangeLabel, rangeText, type PlayerRange } from "@/lib/projection-ranges";
import styles from "./RangeLine.module.css";

/** The floor–ceiling range on its own small line under a projection; nothing when there is no range. */
export function RangeLine({ range, inline = false }: { range: PlayerRange | null | undefined; inline?: boolean }) {
  if (!range) return null;
  return (
    <span className={inline ? styles.inline : styles.line} title={RANGE_TITLE}>
      <span aria-hidden="true">{rangeText(range)}</span>
      <VisuallyHidden>{rangeLabel(range)}</VisuallyHidden>
    </span>
  );
}
