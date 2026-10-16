import { Notice } from "@/components/ui";
import { plural } from "@/lib/plural";
import { tidyReason } from "@/lib/reason-text";
import { reasonCounts, type MoversResponse } from "@/lib/projection-movers";
import styles from "./movers.module.css";

const stop = (s: string) => s.replace(/[.\s]+$/, "");

/** Ours only: the forecasts are experimental beyond next week, and the model is not shown to beat ESPN. */
export function OursNotices({ d }: { d: MoversResponse }) {
  if (d.source !== "independent") return null;
  const g = d.governance;
  const claimed = g?.espn_superiority_established === true;
  return (
    <>
      {d.experimental && (
        <Notice tone="caution" title="Experimental forecasts">
          The earlier forecast of each compared week was made from further back than the next week, and the model is only
          validated for the next week, so treat these movements as exploratory.
        </Notice>
      )}
      {!claimed && (
        <Notice tone="info" title="Not shown to be better than ESPN">
          Fantasy Intel&apos;s model is in use by owner approval, not because it was shown to be more accurate than ESPN
          {g && !g.start_sit_ready ? ", and no position is ready for Start/Sit decisions" : ""}. These movements show how its own
          outlook changed, not whether the outlook is right.
        </Notice>
      )}
    </>
  );
}

/** The caveats that limit the numbers, in one notice: notes, scoring, weeks left out, runs not used. */
export function AboutComparison({ d }: { d: MoversResponse }) {
  const items: string[] = [];
  for (const n of d.notes) items.push(tidyReason(n));
  if (!d.window.scoring_verified) {
    items.push(
      d.window.scoring_note
        ? tidyReason(d.window.scoring_note)
        : "League scoring is not checked between these forecasts, so a scoring change would show up as movement.",
    );
  }
  for (const w of d.window.excluded_weeks) items.push(`Week ${w.week} is left out: ${stop(tidyReason(w.reason))}.`);
  for (const r of reasonCounts(d.skipped)) {
    items.push(`${r.n} ${plural(r.n, "forecast run was", "forecast runs were")} not used: ${stop(tidyReason(r.reason))}.`);
  }
  for (const r of reasonCounts(d.excluded_runs)) {
    items.push(`${r.n} ${plural(r.n, "run was", "runs were")} not compared: ${stop(tidyReason(r.reason))}.`);
  }
  if (items.length === 0) return null;
  return (
    <Notice tone="info" title="About this comparison">
      <ul className={styles.aboutList}>
        {items.map((t, i) => <li key={i}>{t}</li>)}
      </ul>
    </Notice>
  );
}
