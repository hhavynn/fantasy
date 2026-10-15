import { Badge, VisuallyHidden } from "@/components/ui";
import { label, num, pct } from "@/lib/format";
import type { Classification } from "@/lib/types";
import styles from "./managers.module.css";

export const DIMENSION_TITLES: Record<string, string> = {
  waiver_aggression: "Waiver aggression",
  roster_churn: "Roster churn",
  quick_drops: "Quick drops",
  hold_patience: "Patience with adds",
  qb_interest: "QB add interest",
  rb_interest: "RB add interest",
  wr_interest: "WR add interest",
  te_interest: "TE add interest",
  rb_hoarding: "RB hoarding",
  qb_streaming: "QB streaming",
  te_streaming: "TE streaming",
  dst_streaming: "D/ST streaming",
  k_streaming: "K streaming",
  injury_response: "Injury replacement",
  rookie_interest: "Rookie interest",
  trade_activity: "Trade activity",
  faab_spending: "FAAB spending",
};

const SHARE_METRICS = new Set(["roster_churn_rate", "quick_drop_rate", "injury_replacement_rate", "rookie_add_rate",
  "faab_spent_pct"]);

export function formatMetric(metric: string, v: number | null | undefined) {
  if (metric.endsWith("_share") || SHARE_METRICS.has(metric) || metric.startsWith("starter_turnover")) return pct(v);
  if (metric === "average_player_hold_length") return v === null || v === undefined ? num(v) : `${v.toFixed(1)}d`;
  return num(v, 2);
}

const CONFIDENCE_TEXT: Record<string, string> = { HIGH: "high confidence", MEDIUM: "medium confidence", LOW: "low confidence" };

/** Confidence as a dot plus (optionally) words: the dot's colour is never the only signal. */
export function ConfDot({ level, withText = false }: { level: string; withText?: boolean }) {
  const tone = level === "HIGH" ? styles.high : level === "MEDIUM" ? styles.medium : styles.low;
  return (
    <span className={styles.conf} title={`Confidence ${level}`}>
      <span className={`${styles.dot} ${tone}`} aria-hidden="true">●</span>
      {withText ? <span className={styles.confText}>{CONFIDENCE_TEXT[level] ?? level.toLowerCase()}</span> : <VisuallyHidden>{CONFIDENCE_TEXT[level] ?? level.toLowerCase()}</VisuallyHidden>}
    </span>
  );
}

export function labelTone(l: string): string {
  return l === "HIGH" ? "info" : l === "MEDIUM" ? "warn" : "";
}

export function ClassBadge({ c }: { c?: Classification }) {
  if (!c) return <span className="missing" title="Not computed">—</span>;
  const tip = `${formatMetricText(c)} · league mean ${c.league_mean?.toFixed(2) ?? "?"} · n=${c.sample_size} (min ${c.min_sample}) · ${c.rule}`;
  return (
    <span className={styles.classBadge} title={tip}>
      <Badge value={c.label === "INSUFFICIENT" ? c.label : label(c.label)} tone={labelTone(c.label)} />
      <span className="faint">{formatMetric(c.metric, c.value)}</span>
      <ConfDot level={c.confidence} />
    </span>
  );
}

function formatMetricText(c: Classification) {
  if (c.value === null) return "no value";
  return `${c.metric} = ${c.value.toFixed(3)}`;
}
