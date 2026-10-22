import Link from "next/link";
import { cx } from "@/lib/cx";
import { when } from "@/lib/format";
import {
  STATUS_LABEL, sourceName, summarizeProvenance, type ProvenanceStatus, type SignalProvenance,
} from "@/lib/signal-provenance";
import { Icon, type NoticeTone } from "./Notice";
import { VisuallyHidden } from "./VisuallyHidden";
import styles from "./ProvenanceBadge.module.css";

const TONE: Record<ProvenanceStatus, NoticeTone> = {
  current: "positive", partial: "caution", stale: "caution", unknown: "caution", unavailable: "negative",
};

/**
 * Where a headline recommendation's data comes from and how old it is (signal-provenance.v1): a compact badge that
 * names the worst status in words ("Stale data · ESPN league data 3d ago"), with a native disclosure (keyboard and
 * screen-reader operable without script) listing every signal's source, capture time, coverage, snapshot and the
 * reason it is missing or superseded. The status is derived as of this render; an unknown time is never shown as
 * fresh. Renders nothing when the response carried no signals (an older backend).
 */
export function ProvenanceBadge({ signals, now, className }: {
  signals: readonly SignalProvenance[];
  /** For tests and stories; defaults to the time of this render. */
  now?: number;
  className?: string;
}) {
  const summary = summarizeProvenance(signals, now);
  if (!summary) return null;
  return (
    <details className={cx(styles.root, className)} data-status={summary.status}>
      <summary className={styles.summary}>
        <Icon tone={TONE[summary.status]} className={cx(styles.icon, styles[TONE[summary.status]])} />
        <span className={styles.text}>{summary.text}</span>
        <span className={styles.more} aria-hidden="true">Sources</span>
        <VisuallyHidden>: show data sources and age</VisuallyHidden>
      </summary>
      <div className={styles.panel}>
        <ul className={styles.list}>
          {summary.rows.map(({ signal: s, status, age, coverage }) => (
            <li key={`${s.signal}-${s.source ?? ""}-${s.label}`} className={styles.row}>
              <p className={styles.rowHead}>
                <Icon tone={TONE[status]} className={cx(styles.icon, styles[TONE[status]])} />
                <span className={styles.label}>{s.label}</span>
                <span className={styles.status}>{STATUS_LABEL[status]}</span>
              </p>
              <dl className={styles.facts}>
                <div><dt>Source</dt><dd>{sourceName(s.source)}</dd></div>
                <div>
                  <dt>Captured</dt>
                  <dd>{s.captured_at ? <>{when(s.captured_at)} · {age}</> : "Unknown: no capture time recorded"}</dd>
                </div>
                {coverage ? <div><dt>Coverage</dt><dd>{coverage}</dd></div> : null}
                {s.reason_unavailable ? <div><dt>Missing</dt><dd>{s.reason_unavailable}</dd></div> : null}
                {s.stale_reason ? <div><dt>Superseded</dt><dd>{s.stale_reason}</dd></div> : null}
                <div>
                  <dt>Snapshot</dt>
                  <dd>{s.snapshot_version ? <code className={styles.version} title={s.snapshot_version}>{s.snapshot_version}</code> : "Unknown"}</dd>
                </div>
              </dl>
            </li>
          ))}
        </ul>
        <p className={styles.foot}>
          Stale means older than {signals[0]?.stale_after_hours ?? 36}h or replaced by a newer sync attempt; missing values are
          shown as missing, never as 0. <Link href="/data">Data quality report →</Link>
        </p>
      </div>
    </details>
  );
}
