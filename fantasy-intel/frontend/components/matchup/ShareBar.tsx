import styles from "./ShareBar.module.css";

/**
 * Two-tone bar: my share of the two totals combined. `share` is 0-100 and sets the segment width
 * (computed geometry). It is a picture of two numbers, not a win probability; the figures and
 * team names are always printed next to it, so colour is never the only signal.
 */
export function ShareBar({ share, meName, oppName, basis }: { share: number; meName: string; oppName: string; basis: "projected" | "actual" }) {
  const mine = Math.round(share * 10) / 10;
  const theirs = Math.round((100 - share) * 10) / 10;
  return (
    <div className={styles.wrap}>
      <div className={styles.bar} role="img" aria-label={`Share of combined ${basis} points: ${meName} ${mine}%, ${oppName} ${theirs}%`}>
        <span className={styles.me} style={{ width: `${share}%` }} />
        <span className={styles.opp} />
      </div>
      <div className={styles.legend} aria-hidden="true">
        <span><i className={styles.meKey} />{mine.toFixed(1)}%</span>
        <span className={styles.caption}>Share of combined {basis} points</span>
        <span>{theirs.toFixed(1)}%<i className={styles.oppKey} /></span>
      </div>
    </div>
  );
}
