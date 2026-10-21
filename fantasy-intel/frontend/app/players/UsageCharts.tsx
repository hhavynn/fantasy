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

const ptsTick = (v: number) => String(Math.round(v * 10) / 10);

/**
 * Week-by-week usage charts chosen by position. A week with no record (bye, inactive, not played)
 * is a gap in the line, never a zero. Players with no NFL usage data (kickers, defenses, unmatched
 * IDs) get ESPN league points against ESPN's projection instead.
 */
export function UsageCharts({ d, profile = ESPN_PROFILE }: { d: Detail; profile?: LeagueProfile }) {
  const asOf = d.forward_projection?.summary.as_of_week ?? null;
  const lastCompleted = asOf ? asOf - 1 : null;

  if (d.weekly.length === 0) return <LeaguePointsChart d={d} asOf={asOf} profile={profile} />;

  const axis = weekAxis(d.weekly.map((w) => w.week), lastCompleted);
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
        <EmptyState title="No usage to chart">This position has no weekly usage measures here. See the game log below.</EmptyState>
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

/** Fallback for players with no NFL usage rows: ESPN league points against ESPN's own projection. */
function LeaguePointsChart({ d, asOf, profile }: { d: Detail; asOf: number | null; profile: LeagueProfile }) {
  // A Sleeper league: its provider publishes no projections, so only its league points are charted.
  const provider = providerName(profile.provider);
  const espn = profile.providerProjections;
  const rows = d.league_points
    .filter((r) => asOf === null || r.scoring_period <= asOf)
    .map((r) => ({ week: r.scoring_period, actual: r.actual_points, projected: r.projected_points }));
  const axis = weekAxis(rows.map((r) => r.week));
  const actual = alignToAxis(rows, axis, (r) => r.actual);
  const projected = espn ? alignToAxis(rows, axis, (r) => r.projected) : axis.map(() => null);
  if (!hasData(actual) && !hasData(projected)) {
    return (
      <Section title="Points by week">
        <EmptyState title="No weekly points to chart">No NFL usage or {provider} weekly points are stored for this player this season.</EmptyState>
      </Section>
    );
  }
  const spec: ChartSpec = { id: "league", title: espn ? "League points vs ESPN projection" : "League points", format: "pts", series: [] };
  const domain = valueDomain([...actual, ...projected], "pts");
  return (
    <Section
      title="Points by week"
      footnote={espn
        ? "No NFL usage data is stored for this player, so the usage charts are replaced by ESPN league points against ESPN's weekly projection. A gap is a week with no value, not a zero."
        : `No NFL usage data is stored for this player, so the usage charts are replaced by ${provider} league points. A gap is a week with no value, not a zero.`}
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
