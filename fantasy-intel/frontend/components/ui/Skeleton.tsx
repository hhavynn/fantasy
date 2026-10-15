import { cx } from "@/lib/cx";
import { Stat, StatStrip } from "./StatStrip";
import styles from "./Skeleton.module.css";

/** A placeholder block. Decorative (hidden from assistive tech); announce loading on the container. */
export function Skeleton({ w = "100%", h = 14, radius = 6, className }: {
  w?: number | string;
  h?: number;
  radius?: number | string;
  className?: string;
}) {
  return <span className={cx(styles.skeleton, className)} style={{ width: w, height: h, borderRadius: radius }} aria-hidden="true" />;
}

const WIDTHS = ["72%", "48%", "86%", "58%", "66%"];

/** A table-shaped placeholder: header row plus `rows` rows of `cols` cells, with varied cell widths. */
export function SkeletonTable({ rows = 6, cols = 5 }: { rows?: number; cols?: number }) {
  return (
    <div className="table-wrap" aria-hidden="true">
      <table>
        <thead>
          <tr>{Array.from({ length: cols }, (_, c) => <th key={c}><Skeleton w={c === 0 ? "60%" : "40%"} h={10} /></th>)}</tr>
        </thead>
        <tbody>
          {Array.from({ length: rows }, (_, r) => (
            <tr key={r}>
              {Array.from({ length: cols }, (_, c) => <td key={c}><Skeleton w={WIDTHS[(r + c * 2) % WIDTHS.length]} h={12} /></td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** A stat-strip-shaped placeholder with `n` stats. */
export function SkeletonStatStrip({ n = 4 }: { n?: number }) {
  return (
    <StatStrip>
      {Array.from({ length: n }, (_, i) => (
        <Stat key={i} label={<Skeleton w={70} h={10} />} value={<Skeleton w={96} h={26} radius={4} />} note={<Skeleton w={56} h={10} />} />
      ))}
    </StatStrip>
  );
}
