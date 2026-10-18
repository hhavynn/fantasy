import { IntervalChart, LineChart, type IntervalTone } from "@/components/charts";
import { Badge, EmptyState, Meter, Method, Pos, Section } from "@/components/ui";
import { cx } from "@/lib/cx";
import { Missing, num, pct, when } from "@/lib/format";
import { plural } from "@/lib/plural";
import {
  DECISION_BADGE, POSITIONS, VERDICT_BADGE, decisionModels, exclusionRows, intervalRows, intervalText, leadGapText,
  rbVolumeRows, signedNum, versionVerdictText, weeklySeries, weeksText,
  type DecisionModel, type DecisionSection, type PerformanceReport, type PositionSummary, type Verdict,
} from "@/lib/projection-performance";
import styles from "./performance.module.css";

const fmt2 = (v: number) => (signedNum(v, 2) ?? "—");

/** Collected so far against the evidence bar: the count, the bar it is measured against, and a data bar. */
function Progress({ n, required, unit }: { n: number; required: number; unit: string }) {
  return (
    <span className={styles.progress}>
      <Meter value={n} max={required} digits={0} label={`${unit} collected (needed: ${required})`} />
      <span className="faint">/ {required}</span>
    </span>
  );
}

export function VerdictBadge({ verdict, reason }: { verdict: Verdict; reason?: string | null }) {
  const b = VERDICT_BADGE[verdict];
  return <Badge value={b.text} tone={b.tone} title={reason ?? undefined} />;
}

function Diff({ v, ci }: { v: number | null; ci: [number, number] | null }) {
  if (v === null) return <Missing why="No paired observations" />;
  return (
    <>
      <span className={styles.strong}>{signedNum(v, 2)}</span>
      {ci && <span className={cx("faint", styles.small)}>95% CI {intervalText(ci)}</span>}
    </>
  );
}

// ----------------------------------------------------------------------------- 2. position cards

function PositionCard({ pos, p, gate }: { pos: string; p: PositionSummary; gate: PerformanceReport["evidence_gate"] }) {
  const lead = leadGapText(p.lead_time.median_gap_h);
  return (
    <article className={styles.card} aria-labelledby={`pos-${pos}`}>
      <header className={styles.cardHead}>
        <h3 id={`pos-${pos}`} className={styles.cardTitle}><Pos value={pos} /> <span className="faint">{p.model_version}</span></h3>
        <VerdictBadge verdict={p.verdict} reason={p.verdict_reason} />
      </header>
      <p className={styles.statement}>{p.statement}</p>
      <dl className={styles.facts}>
        <div><dt>MAE ours / ESPN</dt><dd>{num(p.ours.mae, 2, "No paired observations")} <span className="faint">/</span> {num(p.espn.mae, 2, "No paired observations")}</dd></div>
        <div><dt>Paired difference</dt><dd><Diff v={p.mae_diff} ci={p.ci95} /></dd></div>
        <div><dt>Fully final weeks</dt><dd><Progress n={p.fully_final_weeks} required={gate.min_weeks} unit="fully final weeks" /></dd></div>
        <div><dt>Matched observations</dt><dd><Progress n={p.paired_n} required={gate.min_matched_per_position} unit="matched observations" /></dd></div>
        <div><dt>Model coverage</dt><dd>{pct(p.coverage.model_share, 0, "No eligible player-weeks")} <span className={cx("faint", styles.small)}>ESPN fallback {pct(p.coverage.fallback_share, 0, "No eligible player-weeks")} · {p.coverage.fallback_n} {plural(p.coverage.fallback_n, "row")}</span></dd></div>
        <div><dt>Forecast timing</dt><dd>{lead ? <>ours {lead}<span className={cx("faint", styles.small)}>median, paired rows</span></> : <Missing why="No paired observations" />}</dd></div>
      </dl>
      <p className={cx("faint", styles.small, styles.cardFoot)} title={p.verdict_reason}>{p.verdict_reason}</p>
    </article>
  );
}

export function PositionCards({ r }: { r: PerformanceReport }) {
  return (
    <Section title="By position" subtitle={`Unconditional, paired with ESPN on the same player-weeks · production model ${r.production.benchmarked_version}`}>
      <div className={styles.cards}>
        {POSITIONS.map((pos) => <PositionCard key={pos} pos={pos} p={r.positions[pos]} gate={r.evidence_gate} />)}
      </div>
    </Section>
  );
}

// ----------------------------------------------------------------------------- 3. ESPN vs ours

const TONE: Record<Verdict, IntervalTone> = {
  insufficient_evidence: "muted", no_significant_difference: "default", ours_better: "positive", espn_better: "negative",
};

function MetricsTable({ r, conditional }: { r: PerformanceReport; conditional?: boolean }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Pos</th>
            <th className="num" title="Paired player-weeks: both projections and the outcome exist">n</th>
            <th className="num">MAE ours</th><th className="num">MAE ESPN</th>
            <th className="num">RMSE ours</th><th className="num">RMSE ESPN</th>
            <th className="num" title="Mean signed error = mean(actual − projection); positive means projected too low">Signed error ours</th>
            <th className="num" title="Mean signed error = mean(actual − projection); positive means projected too low">Signed error ESPN</th>
            <th className="num" title="MAE(ours) − MAE(ESPN); negative means ours closer">Diff</th>
            <th>95% CI</th>
            <th>Verdict</th>
          </tr>
        </thead>
        <tbody>
          {POSITIONS.map((pos) => {
            const p = r.positions[pos];
            const g = conditional ? p.conditional : p;
            const n = conditional ? p.conditional.n : p.paired_n;
            return (
              <tr key={pos}>
                <td><Pos value={pos} /></td>
                <td className="num">{n}</td>
                <td className="num">{num(g.ours.mae, 2, "No paired observations")}</td>
                <td className="num">{num(g.espn.mae, 2, "No paired observations")}</td>
                <td className="num">{num(g.ours.rmse, 2, "No paired observations")}</td>
                <td className="num">{num(g.espn.rmse, 2, "No paired observations")}</td>
                <td className="num">{signedNum(g.ours.bias, 2) ?? <Missing why="No paired observations" />}</td>
                <td className="num">{signedNum(g.espn.bias, 2) ?? <Missing why="No paired observations" />}</td>
                <td className="num">{signedNum(g.mae_diff, 2) ?? <Missing why="No paired observations" />}</td>
                <td className={styles.nowrap}>{intervalText(g.ci95) ?? <Missing why="No interval without paired observations" />}</td>
                <td><VerdictBadge verdict={g.verdict} reason={g.verdict_reason} /></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function EspnComparison({ r }: { r: PerformanceReport }) {
  const rows = intervalRows(r);
  if (rows.every((d) => d.n === 0)) {
    return (
      <Section title="Fantasy Intel vs ESPN" subtitle="Paired MAE difference per position, fully final weeks only">
        <EmptyState title="No paired observations yet">
          A player-week is paired once the week is fully final and both ESPN and the model projected him before his own
          kickoff. Error metrics, the paired difference and its 95% interval appear here from then on.
        </EmptyState>
      </Section>
    );
  }
  return (
    <Section title="Fantasy Intel vs ESPN" subtitle="Paired MAE difference per position, fully final weeks only" flush
      footnote="Hollow dot and dashed interval: not enough evidence for a verdict, whatever the point estimate says.">
      <div className={styles.padded}>
        <IntervalChart
          data={rows.map((d) => ({ label: d.label, value: d.value, lo: d.lo, hi: d.hi, tone: TONE[d.verdict], note: `n ${d.n}` }))}
          valueFormat={fmt2}
          lowLabel="Ours closer"
          highLabel="ESPN closer"
          ariaLabel="MAE difference, ours minus ESPN, by position"
          caption="MAE(ours) − MAE(ESPN) in league points per player-week, with the 95% interval that resamples whole NFL weeks."
        />
      </div>
      <h3 className={styles.subhead}>Unconditional (headline): every resolved player-week, a player who did not play scores 0</h3>
      <MetricsTable r={r} />
      <h3 className={styles.subhead}>Conditional on playing: only players who appeared (reported separately, never the headline)</h3>
      <MetricsTable r={r} conditional />
    </Section>
  );
}

// ----------------------------------------------------------------------------- 3b. RB by volume

function RbVolumeTable({ r, comparison }: { r: PerformanceReport; comparison: "pure_independent" | "conditional" }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Pregame volume</th>
            <th className="num" title="Paired RB player-weeks in this group">n</th>
            <th className="num">MAE ours</th><th className="num">MAE ESPN</th>
            <th className="num" title="MAE(ours) − MAE(ESPN); negative means ours closer">Diff</th>
            <th>95% CI</th>
            <th>Verdict</th>
          </tr>
        </thead>
        <tbody>
          {rbVolumeRows(r, comparison).map((g) => (
            <tr key={g.group}>
              <td>{g.label}</td>
              <td className="num">{g.n}</td>
              <td className="num">{num(g.ours.mae, 2, "No paired observations")}</td>
              <td className="num">{num(g.espn.mae, 2, "No paired observations")}</td>
              <td className="num">{signedNum(g.mae_diff, 2) ?? <Missing why="No paired observations" />}</td>
              <td className={styles.nowrap}>{intervalText(g.ci95) ?? <Missing why="No interval without paired observations" />}</td>
              <td><VerdictBadge verdict={g.verdict} reason={g.verdict_reason} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function RbVolume({ r }: { r: PerformanceReport }) {
  const s = r.rb_volume_split;
  if (!s) return null;
  return (
    <Section title="Running backs by volume" flush
      subtitle={`RB accuracy split at ${s.threshold} carries + targets a game, paired with ESPN on the same player-weeks`}
      footnote={`Opportunities are carries plus targets per game over the back's last five games this season (or fewer), all before ` +
        "the week being scored. Each group needs the same evidence as a position before it gets a verdict."}>
      <p className={styles.plain}>
        Our model is expected to lose to simpler forecasts on backs who rarely touch the ball and to win on backs with real
        volume, so the pooled RB figure above mixes two opposite effects.
      </p>
      <h3 className={styles.subhead}>Unconditional (headline): every resolved player-week, a back who did not play scores 0</h3>
      <RbVolumeTable r={r} comparison="pure_independent" />
      <h3 className={styles.subhead}>Conditional on playing: only backs who appeared</h3>
      <RbVolumeTable r={r} comparison="conditional" />
    </Section>
  );
}

// ----------------------------------------------------------------------------- 4. weekly trend

export function WeeklyTrend({ r }: { r: PerformanceReport }) {
  const weeks = r.weeks.fully_final;
  const pre = r.preliminary.weeks;
  return (
    <Section title="Weekly error trend" subtitle={`MAE per fully final week, ${r.production.benchmarked_version} vs ESPN on the same player-weeks`}
      footnote={pre.length ? `Week ${pre.join(", ")}: not fully final, so not charted (preliminary, no verdict).` : undefined}>
      {weeks.length === 0 ? (
        <EmptyState title="No fully final week yet">
          A week counts once every game is final and every team&apos;s box scores and snaps are synced.
          {pre.length ? ` Week ${pre.join(", ")} is in progress.` : ""}
        </EmptyState>
      ) : (
        <div className={styles.trends}>
          {POSITIONS.map((pos) => {
            const s = weeklySeries(r, pos);
            const total = s.n.reduce((a, b) => a + b, 0);
            return (
              <LineChart
                key={pos}
                title={<><Pos value={pos} /> MAE by week</>}
                xLabels={s.weeks.map((w) => `W${w}`)}
                series={[
                  { id: "ours", label: "Ours", color: "var(--chart-1)", points: s.ours },
                  { id: "espn", label: "ESPN", color: "var(--chart-2)", points: s.espn },
                ]}
                markers
                width={360}
                height={180}
                yFormat={(v) => v.toFixed(1)}
                ariaLabel={`${pos} mean absolute error by week, ours and ESPN`}
                caption={total ? `${total} paired player-weeks; one week is descriptive only` : "No paired player-weeks for this position"}
              />
            );
          })}
        </div>
      )}
    </Section>
  );
}

// ----------------------------------------------------------------------------- 5. model versions

export function ModelVersions({ r }: { r: PerformanceReport }) {
  const mv = r.model_versions;
  const c = mv.comparison;
  const [first, second] = c.versions;
  const weekRows = [...r.weeks.with_espn_history, ...r.weeks.upcoming];      // the next week shows what is captured
  const at = (v: string, w: number) => mv.pregame_by_week[v]?.find((x) => x.week === w);
  const served = (w: number) => mv.served_now.find((x) => x.week === w);
  return (
    <Section title="Model versions" subtitle={`${first} (production) and ${second} (fallback), never pooled`} flush
      footnote={c.rule}>
      {weekRows.length === 0 ? (
        <div className={styles.padded}><EmptyState title="No week with stored ESPN projections yet" /></div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Week</th>
                <th title="What the site shows for the week now (the newest verified run)">Shown now</th>
                <th className="num" title={`Player-weeks with a verified pregame ${first} forecast paired with ESPN`}>{first} pregame</th>
                <th className="num" title="Of those, scored (resolved, fully final week)">scored</th>
                <th className="num" title={`Player-weeks with a verified pregame ${second} forecast paired with ESPN`}>{second} pregame</th>
                <th className="num">scored</th>
              </tr>
            </thead>
            <tbody>
              {weekRows.map((w) => {
                const s = served(w);
                return (
                  <tr key={w}>
                    <td>{w}{r.weeks.upcoming.includes(w) ? <span className={cx("faint", styles.small)}>upcoming</span>
                      : r.weeks.partial.includes(w) ? <span className={cx("faint", styles.small)}>not final</span> : null}</td>
                    <td>
                      {s?.served_version ? <>{s.served_version}{s.fallback ? <> <Badge value="Fallback" tone="warn" /></> : null}</> : <span className="faint" title={s?.note ?? undefined}>ESPN</span>}
                      {s?.forecast_horizon ? (
                        <span className={cx("faint", styles.small)}>
                          rest-of-season forecast, {s.forecast_horizon} {plural(s.forecast_horizon, "week")} ahead (not scored)
                        </span>
                      ) : null}
                    </td>
                    <td className="num">{at(first, w)?.paired_pregame_forecasts ?? 0}</td>
                    <td className="num">{at(first, w)?.scored ?? 0}</td>
                    <td className="num">{at(second, w)?.paired_pregame_forecasts ?? 0}</td>
                    <td className="num">{at(second, w)?.scored ?? 0}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <h3 className={styles.subhead}>
        Head to head on weeks where both have valid pregame forecasts
        {c.common_weeks.length ? ` (week ${c.common_weeks.join(", ")})` : ""}
      </h3>
      {c.n === 0 ? (
        <p className={styles.plain}>No player-week has a valid pregame forecast from both versions, so they are not compared.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Pos</th><th className="num">n</th><th className="num">Weeks</th>
                <th className="num">MAE {first}</th><th className="num">MAE {second}</th><th className="num">MAE ESPN</th>
                <th className="num" title={c.diff}>Diff</th><th>95% CI</th><th>Verdict</th>
              </tr>
            </thead>
            <tbody>
              {POSITIONS.map((pos) => {
                const g = c.by_position[pos];
                const v = g.verdict;
                return (
                  <tr key={pos}>
                    <td><Pos value={pos} /></td>
                    <td className="num">{g.n}</td>
                    <td className="num">{g.weeks_covered}</td>
                    <td className="num">{num(g.mae[first], 2, "No common player-weeks")}</td>
                    <td className="num">{num(g.mae[second], 2, "No common player-weeks")}</td>
                    <td className="num">{num(g.mae.espn, 2, "No common player-weeks")}</td>
                    <td className="num">{signedNum(g.mae_diff, 2) ?? <Missing why="No common player-weeks" />}</td>
                    <td className={styles.nowrap}>{intervalText(g.ci95) ?? <Missing why="No common player-weeks" />}</td>
                    <td><Badge value={versionVerdictText(v, c.versions)} tone="" title={g.verdict_reason} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}

// ----------------------------------------------------------------------------- 6. Start/Sit

function DecisionRow({ label, s }: { label: string; s: DecisionSection }) {
  const b = DECISION_BADGE[s.verdict];
  return (
    <tr>
      <td>{label}</td>
      <td className="num">{s.decisions}</td>
      <td className="num">{s.weeks}</td>
      <td className="num">{pct(s.ours_accuracy, 1, "No decisions")}</td>
      <td className="num">{pct(s.espn_accuracy, 1, "No decisions")}</td>
      <td className="num">{s.difference === null ? <Missing why="No decisions" /> : `${signedNum(s.difference * 100, 1)} pp`}</td>
      <td className={styles.nowrap}>{s.ci95 ? `${signedNum(s.ci95[0] * 100, 1)} to ${signedNum(s.ci95[1] * 100, 1)} pp` : <Missing why="No interval" />}</td>
      <td><Badge value={b.text} tone={b.tone} title={s.statement} /></td>
    </tr>
  );
}

function DecisionTable({ m, gate }: { m: DecisionModel; gate: { min_decisions: number; min_weeks: number } }) {
  if (!m.evaluated || !m.same_position || !m.flex || !m.close_calls || !m.lineups) {
    return <p className={styles.plain}>{m.model_version}: not evaluated ({m.unavailable_reason ?? "no pregame predictions"}).</p>;
  }
  const lu = m.lineups;
  const lb = DECISION_BADGE[lu.verdict];
  return (
    <>
      <div className={styles.padded}>
        <div className={styles.gateRow}>
          <span className={styles.gateLabel}>Same-position decisions</span><Progress n={m.same_position.decisions} required={gate.min_decisions} unit="decisions" />
          <span className={styles.gateLabel}>Weeks</span><Progress n={m.same_position.weeks} required={gate.min_weeks} unit="weeks" />
        </div>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Decision</th><th className="num">n</th><th className="num">Weeks</th>
              <th className="num">Ours right</th><th className="num">ESPN right</th>
              <th className="num" title="Accuracy difference, ours minus ESPN, in percentage points">Diff</th><th>95% CI</th><th>Verdict</th>
            </tr>
          </thead>
          <tbody>
            <DecisionRow label="Same-position pairs" s={m.same_position} />
            <DecisionRow label="FLEX pairs" s={m.flex} />
            <DecisionRow label="Close calls (within 3 pts)" s={m.close_calls} />
          </tbody>
        </table>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Lineups</th><th className="num">n</th><th className="num">Weeks</th>
              <th className="num" title="Points left on the bench vs the hindsight-best lineup, mean">Regret ours</th>
              <th className="num">Regret ESPN</th>
              <th className="num" title="Mean regret, ours minus ESPN; negative means ours left fewer points on the bench">Diff</th><th>95% CI</th><th>Verdict</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>Whole skill lineup</td>
              <td className="num">{lu.decisions}</td>
              <td className="num">{lu.weeks}</td>
              <td className="num">{num(lu.ours_mean_regret, 2, "No lineups")}</td>
              <td className="num">{num(lu.espn_mean_regret, 2, "No lineups")}</td>
              <td className="num">{signedNum(lu.regret_difference, 2) ?? <Missing why="No lineups" />}</td>
              <td className={styles.nowrap}>{intervalText(lu.ci95) ?? <Missing why="No interval" />}</td>
              <td><Badge value={lb.text} tone={lb.tone} title={lu.statement} /></td>
            </tr>
          </tbody>
        </table>
      </div>
    </>
  );
}

export function StartSit({ r }: { r: PerformanceReport }) {
  const s = r.start_sit;
  return (
    <Section title="Start/Sit utility" flush
      subtitle={`Genuine pregame roster decisions only · verdict needs ${s.gate.min_decisions} decisions and ${s.gate.min_weeks} weeks`}
      footnote="A decision counts only when ESPN and the model both projected every player in it before kickoff; where the site showed ESPN's number (fallback), the decision is not credited to the model.">
      {s.sets === 0 ? (
        <div className={styles.padded}>
          <EmptyState title="No pregame decision sets recorded yet">
            The scheduled refresh freezes each week&apos;s rosters and every model&apos;s pregame projections before kickoff.
            Decisions are scored once those games are final.
          </EmptyState>
        </div>
      ) : (
        <>
          <p className={styles.plain}>
            {s.sets} {plural(s.sets, "decision set")} recorded for {plural(s.weeks_recorded.length, "week")} {weeksText(s.weeks_recorded)}
            {s.audit_only_ignored ? ` (${s.audit_only_ignored} audit-only ${plural(s.audit_only_ignored, "set")} ignored)` : ""}. Population: {s.population}.
          </p>
          {decisionModels(s, r.production.production_version).map(([key, m]) => (
            <div key={key} className={styles.block}>
              <h3 className={styles.subhead}>{m.model_version}{m.model_version === r.production.production_version ? " (production)" : " (fallback)"}</h3>
              <DecisionTable m={m} gate={s.gate} />
            </div>
          ))}
        </>
      )}
      {s.warnings.length > 0 && (
        <div className={styles.padded}>
          <ul className={styles.list}>
            {s.warnings.map((w) => <li key={w}>{w}</li>)}
          </ul>
        </div>
      )}
    </Section>
  );
}

// ----------------------------------------------------------------------------- 7. coverage

export function Coverage({ r }: { r: PerformanceReport }) {
  const all = exclusionRows(r.coverage.by_exclusion_reason);
  return (
    <Section title="Coverage and exclusions" flush
      subtitle={`${r.coverage.rows} player-weeks considered, ${r.coverage.scored} scored; every exclusion has exactly one reason`}>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Pos</th>
              <th className="num" title="Player-weeks known to ESPN or the model">Considered</th>
              <th className="num" title="Scored on the headline comparison">Paired</th>
              <th className="num">Excluded</th>
              <th className="num" title="Resolved, fully final player-weeks the site could show">Eligible</th>
              <th className="num" title="Of eligible player-weeks, projected by the model">Model coverage</th>
              <th className="num" title="Of eligible player-weeks, shown with ESPN's number because the model had none">ESPN fallback</th>
              <th>Largest exclusion</th>
            </tr>
          </thead>
          <tbody>
            {POSITIONS.map((pos) => {
              const p = r.positions[pos];
              const top = exclusionRows(p.observations.by_reason)[0];
              return (
                <tr key={pos}>
                  <td><Pos value={pos} /></td>
                  <td className="num">{p.observations.rows}</td>
                  <td className="num">{p.paired_n}</td>
                  <td className="num">{p.observations.excluded}</td>
                  <td className="num">{p.coverage.eligible_resolved}</td>
                  <td className="num">{pct(p.coverage.model_share, 0, "No eligible player-weeks")}</td>
                  <td className="num">{pct(p.coverage.fallback_share, 0, "No eligible player-weeks")}</td>
                  <td className={styles.wrap}>{top ? <>{top.label} <span className="faint">({top.n})</span></> : <span className="faint">None</span>}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <Method title="Every exclusion reason and excluded forecast run">
        {all.length === 0 ? <p className={styles.bare}>No player-week was excluded.</p> : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Reason</th><th className="num">Player-weeks</th></tr></thead>
              <tbody>{all.map((x) => <tr key={x.code}><td title={x.code}>{x.label}</td><td className="num">{x.n}</td></tr>)}</tbody>
            </table>
          </div>
        )}
        {r.coverage.excluded_runs.length > 0 && (
          <ul className={styles.list}>
            {r.coverage.excluded_runs.map((e) => <li key={`${e.run_id}-${e.reason}`}>Week {e.week}, run <span className="mono">{e.run_id}</span>: {e.reason}</li>)}
          </ul>
        )}
      </Method>
    </Section>
  );
}

// ----------------------------------------------------------------------------- 8. methodology

export function Methodology({ r, sha }: { r: PerformanceReport; sha: string | null }) {
  const g = r.evidence_gate;
  return (
    <Section title="Methodology and limitations">
      <ul className={styles.list}>
        <li>Evidence bar per position: at least {g.min_weeks} fully final weeks and {g.min_matched_per_position} matched observations. That is a minimum, not a promise that a real difference will show.</li>
        <li>Interval: {g.interval} ({g.resamples} resamples, fixed seed). A verdict in either direction needs the whole interval on one side of 0.</li>
        <li>Start/Sit: at least {g.start_sit_min_decisions} decisions and {g.start_sit_min_weeks} weeks before any verdict.</li>
        {r.notes.map((n) => <li key={n}>{n}</li>)}
      </ul>
      <Method title="Report provenance">
        <ul className={styles.list}>
          <li>Built {when(r.generated_at)} from the research comparison ({r.benchmark.schema_version}, forecasts from {r.benchmark.forecast_source ?? "none"}); scorecard sha256 <span className="mono">{r.benchmark.content_sha256.slice(0, 16)}</span>.</li>
          {sha && <li>Published report sha256 <span className="mono">{sha.slice(0, 16)}</span>, verified on every request.</li>}
          <li>Data as of: ESPN {when(r.data_as_of.espn)}, NFL {when(r.data_as_of.nfl)}.</li>
        </ul>
      </Method>
    </Section>
  );
}
