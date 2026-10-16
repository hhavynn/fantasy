import Link from "next/link";
import type { CSSProperties } from "react";
import { BackendProblem } from "@/components/BackendProblem";
import { BarChart, LineChart, ScatterChart } from "@/components/charts";
import { Badge, Delta, EmptyState, Method, Notice, PageHeader, Section, SortableTh, Stat, StatStrip } from "@/components/ui";
import { api } from "@/lib/api";
import { cx } from "@/lib/cx";
import type { LlBrief, LlCounterfactual, LlLeader, LlRecord, LlResult, LlScheduleRow, LlTimeline, LuckLab } from "@/lib/luck-types";
import { heatOf, luckBars, periodLabel, scatterPoints, signedText, timelineSeries } from "@/lib/luck-view";
import { parseSort, sortRows, type SearchParams, type SortState } from "@/lib/sort";
import { ScheduleSelector } from "./ScheduleSelector";
import styles from "./luck.module.css";
import { plural } from "@/lib/plural";

export const metadata = { title: "Luck lab" };

// Luck vs. Skill Lab: expected wins from all-play, schedule luck (actual minus expected), schedule
// difficulty, notable results and counterfactual opponent schedules, over completed, fully observed
// regular-season periods only. Historical and explanatory: no probabilities, no projections.

const FLAG_TEXT: Record<string, string> = {
  top_band_loss: "top-band scorer lost", above_median_loss: "above-median loss", lost_to_top_scorer: "lost to the week's top scorer",
  bottom_band_win: "bottom-band scorer won", below_median_win: "below-median win",
};
const RESULT_TONE: Record<LlResult, string> = { W: "good", L: "bad", T: "warn" };
const flagText = (flags: string[]) => flags.map((f) => FLAG_TEXT[f] ?? f).join(", ");

const BOARD_SORTS = ["rank", "team", "actual", "expected", "luck", "allplay", "opp", "periods"] as const;
const BOARD_DEFAULT: SortState = { key: "rank", dir: "asc" };
const SCHED_SORTS = ["team", "periods", "actual", "hyp", "diff"] as const;
const SCHED_DEFAULT: SortState = { key: "team", dir: "asc" };

const one = (v: string | string[] | undefined): string | undefined => (Array.isArray(v) ? v[0] : v);

function rec(r: LlRecord | { wins: number | null; losses: number | null; ties: number | null } | undefined | null): string {
  if (!r || r.wins === null || r.losses === null) return "—";
  return `${r.wins}-${r.losses}${r.ties ? `-${r.ties}` : ""}`;
}

const pctText = (v: number | null | undefined, digits = 0) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(digits)}%`);

export default async function LuckLabPage({ searchParams }: PageProps<"/luck-lab">) {
  const sp = await searchParams;
  const team = one(sp.team);
  // A schedule equal to the focus team is meaningless (the API rejects it): drop it rather than show an error.
  // When the focus team is the default (yours), the page cannot know it up front, so retry without the schedule.
  let vs = one(sp.vs);
  if (vs && team && vs === team) vs = undefined;
  const q = new URLSearchParams();
  if (team) q.set("team_id", team);
  if (vs) q.set("schedule_of", vs);
  let res = await api<LuckLab>(`/api/luck-lab?${q.toString()}`);
  let cleared = one(sp.vs) !== undefined && one(sp.vs) !== "" && vs === undefined;
  if (!res.ok && vs) {
    q.delete("schedule_of");
    const retry = await api<LuckLab>(`/api/luck-lab?${q.toString()}`);
    if (retry.ok && retry.data.focus_team_id === Number(vs)) {
      res = retry;
      cleared = true;
    }
  }
  if (!res.ok) {
    return (
      <div className="stack">
        <PageHeader title="Luck vs. skill lab" />
        <BackendProblem error={res.error} kind={res.kind} status={res.status} />
      </div>
    );
  }
  return <Body d={res.data} cleared={cleared} sp={sp} />;
}

function Body({ d, cleared, sp }: { d: LuckLab; cleared: boolean; sp: SearchParams }) {
  const name = (id: number | null | undefined) => d.teams.find((t) => t.team_id === id)?.name ?? `Team ${id}`;
  const cov = d.coverage;
  const pending = cov.excluded_periods.filter((e) => e.kind === "not_completed").map((e) => e.matchup_period);
  const gaps = cov.excluded_periods.filter((e) => e.kind !== "not_completed");
  const s = d.spotlight;
  const eligible = cov.eligible_periods.length > 0;
  return (
    <div className="stack">
      <PageHeader
        eyebrow="League"
        title="Luck vs. skill lab"
        meta={<>
          {s.name}{s.is_mine ? " (default team)" : ""} · <b>{cov.eligible_periods.length}</b> completed, fully observed regular-season {plural(cov.eligible_periods.length, "period")} of {cov.teams} teams
          {pending.length > 0 && <> · not completed yet: period{pending.length > 1 ? "s" : ""} {pending.join(", ")}</>}
          {" "}· historical and explanatory, not a prediction
        </>}
        actions={
          <ScheduleSelector
            key={`${d.focus_team_id}-${d.schedule_of ?? ""}`}
            teams={d.teams}
            focusTeamId={d.focus_team_id}
            scheduleOf={d.schedule_of}
          >
            <span className={styles.crosslinks}>
              <Link href="/playoff-race">Playoff race</Link> · <Link href="/">Standings</Link>
            </span>
          </ScheduleSelector>
        }
      />

      {cleared && <Notice tone="caution">The schedule comparison was cleared: a team cannot be compared against its own schedule. Pick another manager&apos;s schedule.</Notice>}
      {gaps.length > 0 && (
        <Notice tone="caution" title="Some periods are excluded for incomplete data">
          Excluded for every team, so pools stay complete: {gaps.map((e) => `period ${e.matchup_period}: ${e.reasons.join("; ")} (${e.teams_with_final_scores}/${e.teams} teams with final scores)`).join(" · ")}
        </Notice>
      )}

      {!eligible ? (
        <Notice tone="caution">No completed, fully observed regular-season period yet: expected wins and schedule luck need every team&apos;s final score in the same period. {cov.rule}</Notice>
      ) : (
        <>
          <Headline d={d} />
          <div className="grid grid-2">
            <ScatterSection d={d} />
            <LuckBarsSection d={d} />
          </div>
          <Leaderboard d={d} sp={sp} />
          <WeekByWeek d={d} name={name} />
          <Notables d={d} name={name} />
          <Explorer d={d} name={name} sp={sp} />
          <Matrix d={d} />
        </>
      )}

      <Method title="How the numbers work">
        <ul className="reasons">
          <li>{d.rules.expected}</li><li>{d.rules.luck}</li><li>{d.rules.eligible}</li><li>{d.rules.counterfactual}</li>
          <li>{d.rules.bands}</li><li>{d.rules.result_source}</li>
          {d.notes.map((n, i) => <li key={i}>{n}</li>)}
          {cov.data_notes.duplicate_ids_merged.length > 0 && <li>Identical duplicate matchup ids counted once: {cov.data_notes.duplicate_ids_merged.join(", ")}.</li>}
          {cov.data_notes.conflicting_ids.length > 0 && <li>Conflicting duplicate matchup ids (their periods excluded): {cov.data_notes.conflicting_ids.join(", ")}.</li>}
          {cov.data_notes.no_period.length > 0 && <li>Matchups without a matchup period are ignored: {cov.data_notes.no_period.length}.</li>}
          {cov.data_notes.playoff_rows > 0 && <li>{cov.data_notes.playoff_rows} playoff {plural(cov.data_notes.playoff_rows, "matchup")} {cov.data_notes.playoff_rows === 1 ? "is" : "are"} never mixed with the regular season.</li>}
        </ul>
      </Method>
    </div>
  );
}

// ---------------------------------------------------------------------- headline --

function Headline({ d }: { d: LuckLab }) {
  const s = d.spotlight;
  if (!s.periods) return <Notice tone="caution">{s.name} has no eligible periods.</Notice>;
  const luck = s.schedule_luck ?? 0;
  return (
    <StatStrip>
      <Stat
        label="Expected wins (all-play)"
        value={s.expected_wins?.toFixed(2)}
        note={<>all-play {rec(s.all_play)} ({pctText(s.all_play?.pct, 1)}) · scoring rank #{s.scoring_rank} of {s.teams_ranked}</>}
      />
      <Stat
        label="Actual wins"
        value={s.actual?.win_equivalents.toFixed(1)}
        note={<>{rec(s.actual)} over {s.periods} eligible {plural(s.periods, "period")} · ESPN {rec(s.espn_record)}</>}
      />
      <Stat
        label="Schedule luck"
        value={`${signedText(luck)} wins`}
        tone={luck > 0 ? "positive" : luck < 0 ? "negative" : undefined}
        note={`#${s.luck_rank} of ${s.teams_ranked} (1 = most actual wins above expected)`}
      />
      <Stat
        label="Schedule difficulty"
        value={`#${s.difficulty_rank}`}
        note={<>toughest of {s.teams_ranked} · opponents averaged {s.difficulty?.opponent_points_mean.toFixed(1)} pts</>}
      />
    </StatStrip>
  );
}

// ------------------------------------------------------------------------ charts --

function ScatterSection({ d }: { d: LuckLab }) {
  return (
    <Section
      title="Actual vs expected wins"
      footnote="Dashed diagonal: actual equals expected. Above it, a team has more wins than its scoring implies. Labelled: your team and teams at least one win off the diagonal. Hover any point for its numbers."
    >
      <ScatterChart
        points={scatterPoints(d.leaderboard)}
        xLabel="Expected wins"
        yLabel="Actual wins"
        diagonal
        xFormat={(v) => v.toFixed(1)}
        yFormat={(v) => v.toFixed(1)}
        ariaLabel="Actual win equivalents against expected wins per team"
      />
    </Section>
  );
}

function LuckBarsSection({ d }: { d: LuckLab }) {
  const bars = luckBars(d.leaderboard);
  return (
    <Section
      title="Schedule luck by team"
      footnote="Wins above (+) or below (−) expected, over the same eligible periods. Positive means the schedule and results favoured the team."
    >
      <BarChart
        data={bars}
        orientation="horizontal"
        valueFormat={(v) => signedText(v)}
        ariaLabel="Schedule luck per team, in wins"
      />
    </Section>
  );
}

// -------------------------------------------------------------------- leaderboard --

function Leaderboard({ d, sp }: { d: LuckLab; sp: SearchParams }) {
  const sort = parseSort(sp, BOARD_SORTS, BOARD_DEFAULT);
  const rows = sortRows(d.leaderboard, (x: LlLeader) => {
    switch (sort.key) {
      case "team": return x.name;
      case "actual": return x.actual.win_equivalents;
      case "expected": return x.expected_wins;
      case "luck": return x.schedule_luck;
      case "allplay": return x.all_play.pct;
      case "opp": return x.opponent_points_mean;
      case "periods": return x.periods;
      default: return x.luck_rank;
    }
  }, sort.dir);
  return (
    <Section
      title="League luck leaderboard"
      flush
      footnote="Actual = win equivalents (a tie counts a half). Expected = all-play win equivalents. Luck = actual minus expected over the same periods. Opp. avg = average opponent score faced (#1 = toughest)."
    >
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <SortableTh sortKey="rank" label="#" align="right" defaultDir="asc" current={sort} searchParams={sp} title="Luck rank (1 = most actual wins above expected)" />
              <SortableTh sortKey="team" label="Team" current={sort} searchParams={sp} />
              <SortableTh sortKey="actual" label="Actual" align="right" current={sort} searchParams={sp} />
              <SortableTh sortKey="expected" label="Expected" align="right" current={sort} searchParams={sp} />
              <SortableTh sortKey="luck" label="Schedule luck" current={sort} searchParams={sp} />
              <SortableTh sortKey="allplay" label="All-play" current={sort} searchParams={sp} />
              <SortableTh sortKey="opp" label="Opp. avg" align="right" current={sort} searchParams={sp} />
              <SortableTh sortKey="periods" label="Periods" align="right" current={sort} searchParams={sp} />
            </tr>
          </thead>
          <tbody>
            {rows.map((x) => (
              <tr key={x.team_id} className={x.is_mine ? "mine" : ""}>
                <td className="num faint">{x.luck_rank}</td>
                <td><Link href={`/luck-lab?team=${x.team_id}`}>{x.name}</Link> <span className="faint">ESPN {rec(x.espn_record)}</span></td>
                <td className="num">{x.actual.win_equivalents.toFixed(1)} <span className="faint">({rec(x.actual)})</span></td>
                <td className="num">{x.expected_wins.toFixed(2)}</td>
                <td><Delta value={x.schedule_luck} digits={2} /></td>
                <td>{rec(x.all_play)} <span className="faint">{pctText(x.all_play.pct)}</span></td>
                <td className="num">{x.opponent_points_mean.toFixed(1)} <span className="faint">#{x.difficulty_rank}</span></td>
                <td className="num">{x.periods}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

// -------------------------------------------------------------------- week by week --

function Result({ r }: { r: LlResult }) {
  return <Badge value={r} tone={RESULT_TONE[r]} />;
}

function WeekByWeek({ d, name }: { d: LuckLab; name: (id: number) => string }) {
  const t = d.timeline;
  if (t.length === 0) return null;
  const s = timelineSeries(t);
  return (
    <Section title={`${d.spotlight.name}: week-by-week luck`}>
      <div className={styles.charts3}>
        <div>
          <h3 className={styles.chartTitle}>Score vs opponent</h3>
          <LineChart
            series={[
              { id: "me", label: d.spotlight.name, points: s.score },
              { id: "opp", label: "Opponent", points: s.opponent, color: "var(--chart-6)" },
            ]}
            xLabels={s.labels}
            markers
            yFormat={(v) => v.toFixed(0)}
            ariaLabel="Team score and opponent score per period"
          />
        </div>
        <div>
          <h3 className={styles.chartTitle}>Luck by period</h3>
          <BarChart data={t.map((x, i) => ({ label: s.labels[i], value: x.luck }))} valueFormat={(v) => signedText(v)} ariaLabel="Luck per period, in wins" />
        </div>
        <div>
          <h3 className={styles.chartTitle}>Cumulative schedule luck</h3>
          <LineChart
            series={[{ id: "cum", label: "Cumulative luck", points: s.cumulative }]}
            xLabels={s.labels}
            markers
            refLine={{ y: 0, label: "0" }}
            yFormat={(v) => signedText(v, 1)}
            ariaLabel="Cumulative schedule luck, in wins"
          />
        </div>
      </div>
      <p className={styles.caption}>Weeks are on the horizontal axes. Luck is the actual result minus expected wins for that period; the cumulative line adds them up. Both are in wins, on the same scale.</p>

      <div className={styles.tableBleed}>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Period</th><th>Result</th><th className="num">Score</th><th>Opponent</th><th className="num">Rank</th>
                <th className="num">Expected</th><th>Luck</th><th className="num">Cumulative</th>
              </tr>
            </thead>
            <tbody>
              {t.map((x) => (
                <tr key={x.matchup_period}>
                  <td>{periodLabel(x.weeks, x.matchup_period)}</td>
                  <td><Result r={x.result} /></td>
                  <td className="num">{x.score.toFixed(1)}</td>
                  <td>{x.opponent_name} <span className="faint">{x.opponent_score.toFixed(1)}</span></td>
                  <td className="num">#{x.rank} <span className="faint">of {x.teams}</span></td>
                  <td className="num">{x.expected_wins.toFixed(2)}</td>
                  <td><Delta value={x.luck} digits={2} /></td>
                  <td className="num">{signedText(x.cumulative_luck)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <h3 className={styles.listTitle}>Weeks that moved the gap most</h3>
      <ul className="reasons">{d.biggest_contributors.map((x: LlTimeline) => (
        <li key={x.matchup_period}>{periodLabel(x.weeks, x.matchup_period)}: <Result r={x.result} /> {x.score.toFixed(1)}–{x.opponent_score.toFixed(1)} vs {name(x.opponent_id)} · #{x.rank} of {x.teams} · expected {x.expected_wins.toFixed(2)} → luck <b>{signedText(x.luck)}</b></li>
      ))}</ul>
    </Section>
  );
}

// ------------------------------------------------------------------------ notables --

function BriefList({ items, kind, name, empty }: { items: LlBrief[]; kind: string; name: (id: number) => string; empty: string }) {
  if (items.length === 0) return <p className={styles.none}>{empty}</p>;
  return (
    <ul className={styles.briefs}>
      {items.map((b) => (
        <li key={b.matchup_period}>
          <span className={styles.briefHead}>{periodLabel(b.weeks, b.matchup_period)}: <b>{b.score.toFixed(1)}</b> (#{b.rank} of {b.teams}) {kind} {b.opponent_score.toFixed(1)} vs {name(b.opponent_id)} (#{b.opponent_rank})</span>
          <span className={styles.briefFlags}>{flagText(b.flags)}</span>
        </li>
      ))}
    </ul>
  );
}

function Notables({ d, name }: { d: LuckLab; name: (id: number) => string }) {
  const s = d.spotlight;
  if (!s.periods) return null;
  const sc = s.scoring;
  const df = s.difficulty;
  return (
    <div className="grid grid-2">
      <Section title="Brutal losses and fortunate wins" footnote="Flags are descriptive: a loss with a top-band score, a win with a bottom-band score, and so on. They are not predictions.">
        <h3 className={styles.listTitle}>Brutal losses</h3>
        <BriefList items={s.notable?.brutal_losses ?? []} kind="lost to" name={name} empty="None in eligible periods." />
        <h3 className={styles.listTitle}>Fortunate wins</h3>
        <BriefList items={s.notable?.fortunate_wins ?? []} kind="beat" name={name} empty="None in eligible periods." />
      </Section>
      <Section title="Scoring consistency" footnote={`Over ${s.periods} eligible ${plural(s.periods, "period")}.`}>
        {sc ? (
          <dl className={styles.kv}>
            <dt>Mean · median</dt><dd>{sc.mean.toFixed(1)} · {sc.median.toFixed(1)}</dd>
            <dt>Range</dt><dd>{sc.min.toFixed(1)}–{sc.max.toFixed(1)}</dd>
            <dt>Middle half</dt><dd>{sc.iqr !== null ? `${sc.q1?.toFixed(1)}–${sc.q3?.toFixed(1)} (IQR ${sc.iqr.toFixed(1)})` : <span className="missing" title="Too few periods for quartiles">—</span>}</dd>
            <dt>Top half of the league</dt><dd>{pctText(sc.top_half_pct)} of periods</dd>
            <dt>First / last place</dt><dd>{sc.first_place} first-place · {sc.last_place} last-place weeks</dd>
            {df && <><dt>Opponent strength</dt><dd>opponents averaged a {pctText(df.opponent_share_mean)} all-play share in the weeks you met them
              {df.opponent_season_share_mean !== null && <> versus {pctText(df.opponent_season_share_mean)} across their whole season</>}</dd>
            <dt>Opponent weeks</dt><dd>{df.opponents_top_band} in the top band · {df.opponents_bottom_band} in the bottom band</dd></>}
          </dl>
        ) : <EmptyState title="No scoring summary" />}
      </Section>
    </div>
  );
}

// -------------------------------------------------------------------- explorer --

function CfTable({ c, name }: { c: LlCounterfactual; name: (id: number) => string }) {
  return (
    <div className="table-wrap">
      <table>
        <thead><tr><th>Period</th><th className="num">Score</th><th>Actual</th><th>Their opponent that week</th><th className="num">Margin</th><th>Hypothetical</th></tr></thead>
        <tbody>{c.rows.map((x) => (
          <tr key={x.matchup_period} className={x.changed ? styles.changed : undefined}>
            <td>{periodLabel(x.weeks, x.matchup_period)}</td>
            <td className="num">{x.score.toFixed(1)}</td>
            <td><Result r={x.actual_result} /> <span className="faint">{x.actual_opponent_score.toFixed(1)} vs {name(x.actual_opponent_id)}</span></td>
            <td>{name(x.substituted_opponent_id)} <span className="faint">{x.substituted_opponent_score.toFixed(1)}</span></td>
            <td className="num">{signedText(x.margin, 1)}</td>
            <td><Result r={x.counterfactual_result} />{x.changed && <b className={styles.changedTag}> changed</b>}</td>
          </tr>))}</tbody>
      </table>
    </div>
  );
}

function Explorer({ d, name, sp }: { d: LuckLab; name: (id: number) => string; sp: SearchParams }) {
  const c = d.counterfactual;
  const sort = parseSort(sp, SCHED_SORTS, SCHED_DEFAULT, "sched");
  const rows = sortRows(d.all_schedules, (r: LlScheduleRow) => {
    switch (sort.key) {
      case "periods": return r.periods_compared.length;
      case "actual": return r.actual.win_equivalents;
      case "hyp": return r.counterfactual.win_equivalents;
      case "diff": return r.win_equivalent_difference;
      default: return r.name;
    }
  }, sort.dir);
  return (
    <Section title="Counterfactual schedule explorer" footnote={d.rules.counterfactual}>
      {c ? (
        <div className={styles.cfBlock}>
          <StatStrip>
            <Stat label="Periods compared" value={c.periods_compared.length} note={c.excluded.length ? `${c.excluded.length} excluded: ${c.excluded.map((e) => e.reason).join("; ")}` : "none excluded"} />
            <Stat label="Actual (same periods)" value={rec(c.actual)} />
            <Stat label={`With ${name(c.schedule_of)}'s opponents`} value={rec(c.counterfactual)} />
            <Stat
              label="Difference"
              value={`${signedText(c.win_equivalent_difference, 1)} wins`}
              tone={c.win_equivalent_difference > 0 ? "positive" : c.win_equivalent_difference < 0 ? "negative" : undefined}
              note={`${c.flips.loss_to_win.length} L→W · ${c.flips.win_to_loss.length} W→L${c.flips.other_changes.length ? ` · ${c.flips.other_changes.length} via ties` : ""}`}
            />
          </StatStrip>
          <div className={styles.cfTable}><CfTable c={c} name={name} /></div>
        </div>
      ) : (
        <p className={styles.none}>Pick a manager above (&ldquo;with the schedule of&rdquo;) to see {d.spotlight.name}&apos;s scores against that manager&apos;s opponents, week by week.</p>
      )}

      <h3 className={styles.listTitle}>{d.spotlight.name} under every other manager&apos;s schedule</h3>
      <div className={styles.tableBleed}>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <SortableTh sortKey="team" label="Schedule of" current={sort} searchParams={sp} param="sched" />
                <SortableTh sortKey="periods" label="Periods" align="right" current={sort} searchParams={sp} param="sched" title="Periods compared (weeks that manager played your team are excluded)" />
                <SortableTh sortKey="actual" label="Actual" align="right" current={sort} searchParams={sp} param="sched" />
                <SortableTh sortKey="hyp" label="Hypothetical" align="right" current={sort} searchParams={sp} param="sched" />
                <SortableTh sortKey="diff" label="Difference" current={sort} searchParams={sp} param="sched" />
                <th className="num">Win % actual → hypothetical</th>
                <th />
              </tr>
            </thead>
            <tbody>{rows.map((r) => (
              <tr key={r.schedule_of}>
                <td>{r.name}</td>
                <td className="num" title={r.excluded.map((e) => `period ${e.matchup_period}: ${e.reason}`).join("\n")}>{r.periods_compared.length}{r.excluded.length ? <span className="faint"> (−{r.excluded.length})</span> : null}</td>
                <td className="num">{rec(r.actual)}</td>
                <td className="num">{rec(r.counterfactual)}</td>
                <td><Delta value={r.win_equivalent_difference} digits={1} suffix=" wins" /></td>
                <td className="num">{r.actual_pct !== null ? `${pctText(r.actual_pct)} → ${pctText(r.counterfactual_pct)}` : "—"}</td>
                <td><Link href={`/luck-lab?team=${d.focus_team_id}&vs=${r.schedule_of}`}>Weeks</Link></td>
              </tr>))}</tbody>
          </table>
        </div>
      </div>
      <p className={styles.caption}>Each row compares actual and hypothetical results on its own periods: the weeks that manager played {d.spotlight.name} are excluded (−n), so rows can cover different periods and are listed by team, not ranked. The win % columns compare like with like within a row.</p>
    </Section>
  );
}

// ---------------------------------------------------------------------- matrix --

function Matrix({ d }: { d: LuckLab }) {
  const m = d.matrix;
  return (
    <Section
      title="Weekly results matrix"
      flush
      footnote="Each cell: weekly scoring rank (#1 = highest) and the actual result (W, L or T). Deeper blue means more of the league was outscored that week (all-play share); a flag marks a brutal loss or fortunate win. Hover for points and expected wins."
    >
      <div className="table-wrap">
        <table className={styles.matrix}>
          <thead><tr><th>Team</th>{m.periods.map((p) => <th key={p.matchup_period} className="num">{p.weeks?.join("–") ?? p.matchup_period}</th>)}</tr></thead>
          <tbody>{m.teams.map((t) => (
            <tr key={t.team_id} className={t.team_id === d.focus_team_id ? "mine" : ""}>
              <td>{t.name}</td>
              {t.cells.map((c) => (
                <td
                  key={c.matchup_period}
                  className={cx("num", styles.heat)}
                  style={{ "--heat": heatOf(c.expected_wins).toFixed(2) } as CSSProperties}
                  title={`Period ${c.matchup_period}: ${c.score.toFixed(1)} pts · #${c.rank} · ${c.result} · expected ${c.expected_wins.toFixed(2)}${c.flags.length ? ` · ${flagText(c.flags)}` : ""}`}
                >
                  #{c.rank} <b className={styles[`r${c.result}`]}>{c.result}</b>
                  {c.flags.length > 0 && <span title={flagText(c.flags)} aria-label={flagText(c.flags)}> ⚑</span>}
                </td>
              ))}
            </tr>))}</tbody>
        </table>
      </div>
    </Section>
  );
}
