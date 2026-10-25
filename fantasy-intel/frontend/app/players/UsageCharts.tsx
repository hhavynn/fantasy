import { LineChart } from "@/components/charts";
import { EmptyState, Section } from "@/components/ui";
import {
  alignToAxis, hasData, pctTick, usageChartSpecs, valueDomain, weekAxis,
  type ChartSpec,
} from "@/lib/player-usage";
import styles from "./players.module.css";
import type { Detail } from "./types";
import { providerName } from "@/lib/labels";
import { ESPN_PROFILE, type LeagueProfile } from "@/lib/league-profile";
import { boxScoreWeeks, inRange, type WeekRange } from "@/lib/player-tabs";

const ptsTick = (v: number) => String(Math.round(v * 10) / 10);

/** The last completed week (the week before the forward view's first), or null when the league window is unknown. */
export function lastCompletedWeek(d: Detail): number | null {
  const asOf = d.forward_projection?.summary.as_of_week ?? null;
  return asOf ? asOf - 1 : null;
}

/**
 * Week-by-week usage charts chosen by position. A week with no record (bye, inactive, not played)
 * is a gap in the line, never a zero. Players with no NFL usage data (kickers, defenses, unmatched
 * IDs) get ESPN league points against ESPN's projection instead. `range` narrows the axis to a week window.
 */
export function UsageCharts({ d, profile = ESPN_PROFILE, range = null }: { d: Detail; profile?: LeagueProfile; range?: WeekRange | null }) {
  const asOf = d.forward_projection?.summary.as_of_week ?? null;
  const lastCompleted = lastCompletedWeek(d);

  if (d.weekly.length === 0) return <LeaguePointsChart d={d} asOf={asOf} profile={profile} range={range} />;

  const axis = weekAxis(d.weekly.map((w) => w.week), lastCompleted).filter((w) => inRange(w, range));
  const labels = axis.map(String);
  const charts = usageChartSpecs(d.player.position).map((spec) => ({
    spec,
    series: spec.series.map((s) => ({
      id: s.id,
      label: s.label,
      points: alignToAxis(d.weekly, axis, (w) => w[s.field]),
    })),
  })).filter((c) => c.series.some((s) => hasData(s.points)));

  if (charts.length === 0) {
    return (
      <Section title="Usage by week">
        {range && d.weekly.length > 0 && usageChartSpecs(d.player.position).length > 0
          ? <EmptyState title="No usage recorded in these weeks">No NFL usage is stored for weeks {range.from}–{range.to}. A week without a record is unknown, not zero; widen the window to see earlier weeks.</EmptyState>
          : <EmptyState title="No usage to chart">This position has no weekly usage measures here. See the game log.</EmptyState>}
      </Section>
    );
  }

  return (
    <Section
      title="Usage by week"
      subtitle="Snaps, shares and opportunity, week by week"
      footnote="A gap in a line is a week with no record (bye, inactive or not yet played). It is not a zero."
    >
      <div className={styles.charts}>
        {charts.map(({ spec, series }) => (
          <div key={spec.id} className={styles.chart}>
            <h3 className={styles.chartTitle}>{spec.title}</h3>
            <LineChart
              caption={`Weeks ${labels[0]}–${labels[labels.length - 1]} · nflverse`}
              series={series}
              xLabels={labels}
              yFormat={spec.format === "pct" ? pctTick : ptsTick}
              yDomain={valueDomain(series.flatMap((s) => s.points), spec.format)}
              height={190}
              width={420}
              markers
              ariaLabel={`${d.player.full_name} ${spec.title.toLowerCase()} by week`}
            />
            {spec.note ? <p className={styles.chartNote}>{spec.note}</p> : null}
          </div>
        ))}
      </div>
    </Section>
  );
}

/**
 * League points by week (against ESPN's weekly projection where the provider publishes one). The fallback for
 * players with no NFL usage rows, and, as `trend`, the profile Overview's recent-form chart for every player.
 */
export function LeaguePointsChart({ d, asOf, profile, range = null, trend = false }: {
  d: Detail; asOf: number | null; profile: LeagueProfile; range?: WeekRange | null;
  /** The Overview's form chart: a lighter title and a footnote that does not talk about missing usage. */
  trend?: boolean;
}) {
  // A Sleeper league: its provider publishes no projections, so only its league points are charted.
  const provider = providerName(profile.provider);
  const espn = profile.providerProjections;
  const rows = d.league_points
    .filter((r) => asOf === null || r.scoring_period <= asOf)
    .map((r) => ({ week: r.scoring_period, actual: r.actual_points, projected: r.projected_points }));
  const axis = weekAxis(rows.map((r) => r.week)).filter((w) => inRange(w, range));
  const actual = alignToAxis(rows, axis, (r) => r.actual);
  const projected = espn ? alignToAxis(rows, axis, (r) => r.projected) : axis.map(() => null);
  const title = trend ? "Recent form" : "Points by week";
  // Weeks Fantasy Intel scored from the NFL box score (#254 S3): named under the chart, never passed off as the provider's
  const boxed = [...boxScoreWeeks(d.league_points)].filter((w) => axis.includes(w)).sort((a, b) => a - b);
  const boxNote = boxed.length > 0
    ? ` ${boxed.length === 1 ? "Week" : "Weeks"} ${boxed.join(", ")} ${boxed.length === 1 ? "was" : "were"} scored by Fantasy Intel from the NFL box score in your league's scoring: ${provider} did not score ${boxed.length === 1 ? "it" : "them"}.`
    : "";
  if (!hasData(actual) && !hasData(projected)) {
    return (
      <Section title={title}>
        <EmptyState title="No weekly points to chart">
          {range ? `No ${provider} weekly points are stored for weeks ${range.from}–${range.to}.` : trend
            ? `No ${provider} weekly points are stored for this player this season.`
            : `No NFL usage or ${provider} weekly points are stored for this player this season.`}
        </EmptyState>
      </Section>
    );
  }
  const spec: ChartSpec = { id: "league", title: espn ? "League points vs ESPN projection" : "League points", format: "pts", series: [] };
  const domain = valueDomain([...actual, ...projected], "pts");
  return (
    <Section
      title={title}
      subtitle={trend ? `${provider} league points by week, in your league's scoring` : undefined}
      footnote={(trend
        ? `A gap is a week with no value (bye, inactive or not played), not a zero.${espn ? " The second line is ESPN's weekly projection for the same week." : ""}`
        : espn
          ? "No NFL usage data is stored for this player, so the usage charts are replaced by ESPN league points against ESPN's weekly projection. A gap is a week with no value, not a zero."
          : `No NFL usage data is stored for this player, so the usage charts are replaced by ${provider} league points. A gap is a week with no value, not a zero.`) + boxNote}
    >
      <div className={styles.chart}>
        <h3 className={styles.chartTitle}>{spec.title}</h3>
        <LineChart
          series={[
            { id: "actual", label: "League points", points: actual },
            ...(espn ? [{ id: "projected", label: "ESPN projection", points: projected }] : []),
          ]}
          xLabels={axis.map(String)}
          yFormat={ptsTick}
          yDomain={domain}
          height={200}
          width={520}
          markers
          ariaLabel={espn ? `${d.player.full_name} league points against ESPN projection by week` : `${d.player.full_name} league points by week`}
        />
      </div>
    </Section>
  );
}
