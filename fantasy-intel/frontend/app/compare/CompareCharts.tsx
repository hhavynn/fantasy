import { LineChart, ScatterChart, type ScatterPoint } from "@/components/charts";
import { seriesColor } from "@/components/charts/shared";
import { EmptyState, Section } from "@/components/ui";
import {
  type ChartSeries, type CompareResponse,
  opportunityPoints, remainingProjectionChart, scoringLabel, weeklyPointsChart,
} from "@/lib/compare";
import styles from "./compare.module.css";

const oneDecimal = (v: number) => v.toFixed(1);

/** The numbers behind a line chart, for screen readers and anyone who prefers a table. */
function ChartData({ xName, xLabels, series }: { xName: string; xLabels: string[]; series: ChartSeries[] }) {
  return (
    <details className={styles.chartData}>
      <summary>Chart data</summary>
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th scope="col">{xName}</th>{series.map((s) => <th key={s.id} scope="col" className="num">{s.label}</th>)}</tr>
          </thead>
          <tbody>
            {xLabels.map((x, i) => (
              <tr key={x}>
                <th scope="row">{x}</th>
                {series.map((s) => (
                  <td key={s.id} className="num">
                    {s.points[i] === null || s.points[i] === undefined ? <span className="missing" title="Bye or no value: a gap, not a zero">—</span> : oneDecimal(s.points[i] as number)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

/** Weekly points, opportunity vs production and the remaining-season projections. Server-rendered SVG. */
export function ChartsSection({ r }: { r: CompareResponse }) {
  const [a, b] = r.players;
  const names = `${a.player.full_name ?? "Player A"} and ${b.player.full_name ?? "Player B"}`;
  const weekly = weeklyPointsChart(r);
  const remaining = remainingProjectionChart(r);
  const points = opportunityPoints(r);
  const scatter: ScatterPoint[] = points.map((p) => ({
    id: p.id, x: p.x, y: p.y, label: p.label, highlight: true, color: seriesColor(p.id === "a" ? 0 : 1),
    title: `${p.label}: expected ${oneDecimal(p.x)}, scored ${oneDecimal(p.y)} PPR per game`,
  }));
  const last = r.completed_through_week;

  return (
    <Section
      title="Visualizations"
      subtitle="Server-drawn charts of the same stored numbers. Player A is blue, Player B is orange; a gap in a line is a bye or a missing value, never a zero."
    >
      <div className={styles.charts}>
        <div className={styles.chartBlock}>
          {weekly ? (
            <>
              <LineChart
                series={weekly.series} xLabels={weekly.xLabels} markers height={240} width={480}
                yFormat={oneDecimal}
                ariaLabel={`Weekly league points, weeks 1 to ${last}, ${names}`}
                title="Weekly league points"
                caption={`${scoringLabel(r.scoring)}, weeks 1 to ${last}. A gap is a bye or a week with no ESPN score.`}
              />
              <ChartData xName="Week" xLabels={weekly.xLabels} series={weekly.series} />
            </>
          ) : (
            <EmptyState title="No weekly points yet">Neither player has an ESPN score in a completed week, so there is no weekly line to draw.</EmptyState>
          )}
        </div>

        <div className={styles.chartBlock}>
          {scatter.length > 0 ? (
            <ScatterChart
              points={scatter} xLabel="Expected PPR / game (xFP)" yLabel="PPR / game" diagonal height={300} width={440}
              xFormat={(v) => v.toFixed(0)} yFormat={(v) => v.toFixed(0)}
              ariaLabel={`Opportunity against production per game, ${names}`}
              title="Opportunity vs production"
              caption={`nflverse full PPR per game through week ${last}. Above the dashed line a player scored more than his workload was worth; below it, less.`}
            />
          ) : (
            <EmptyState title="No opportunity chart">A point needs both expected PPR and PPR per game, which come from nflverse usage records. Neither player has both.</EmptyState>
          )}
        </div>

        <div className={styles.chartBlock}>
          {remaining ? (
            <>
              <LineChart
                series={remaining.espn} xLabels={remaining.xLabels} markers height={240} width={480}
                yFormat={oneDecimal}
                ariaLabel={`ESPN projected points for the remaining weeks, ${names}`}
                title="Remaining weeks: ESPN projections"
                caption="ESPN weekly projections from this week on. A gap is a bye week or a week without a projection."
              />
              <ChartData xName="Week" xLabels={remaining.xLabels} series={remaining.espn} />
            </>
          ) : (
            <EmptyState title="No remaining-season projections">The league&apos;s projection window is unknown or neither player has an ESPN projection for the remaining weeks.</EmptyState>
          )}
        </div>

        {remaining?.ours ? (
          <div className={styles.chartBlock}>
            <LineChart
              series={remaining.ours} xLabels={remaining.xLabels} markers height={240} width={480}
              yFormat={oneDecimal}
              ariaLabel={`Ours projected points for the remaining weeks, ${names}`}
              title="Remaining weeks: Ours"
              caption="Fantasy Intel's own numbers, drawn only where the model produced one: ESPN stand-ins and byes are gaps."
            />
            <ChartData xName="Week" xLabels={remaining.xLabels} series={remaining.ours} />
          </div>
        ) : null}
      </div>
    </Section>
  );
}
