import type { ReactNode } from "react";
import { PlayerLink } from "@/components/PlayerLink";
import {
  Badge, EmptyState, Method, Notice, Pos, Section, Skeleton, SkeletonTable, Tabs, Verdict, VerdictFigure,
} from "@/components/ui";
import type { ApiResult } from "@/lib/api";
import {
  costRangeText, costText, exclusionLines, hitRateText, inputLines, outcomeText, pts, tallyText, utcText,
  VERDICT_LABEL, VERDICT_TONE,
  type DecisionFeedback, type DfCall, type DfPlayer, type DfSeasonWeek, type DfSource, type DfWeek,
} from "@/lib/decision-feedback";
import { plural } from "@/lib/plural";
import { recapHref, type RecapQuery } from "@/lib/weekly-recap";
import styles from "./weekly-recap.module.css";

// Decision feedback for the viewer's own team: start/sit calls implied by inputs archived before kickoff, graded against
// the week's points. Every number and every grade comes from the backend; this file only lays them out.

const TITLE = "Your decisions";

export function DecisionFeedbackSkeleton() {
  return (
    <Section title={TITLE} subtitle={<Skeleton w={260} h={12} />} flush>
      <div role="status" aria-busy="true"><SkeletonTable rows={4} cols={5} /></div>
    </Section>
  );
}

export async function DecisionFeedbackSection({ pending, q, providerProjections }: {
  pending: Promise<ApiResult<DecisionFeedback>>;
  q: RecapQuery;
  providerProjections: boolean;
}) {
  const res = await pending;
  if (!res.ok) {
    return (
      <Section title={TITLE}>
        <Notice tone="caution" title="Decision feedback is unavailable">{res.error}</Notice>
      </Section>
    );
  }
  const d = res.data;
  if (d.status === "no_completed_week") return null;
  const sources = <SourceTabs d={d} q={q} providerProjections={providerProjections} />;
  if (d.status === "no_team" || d.status === "weeks_unmapped") {
    return (
      <Section title={TITLE}>
        <EmptyState title={d.status === "no_team" ? "Your team is not set for this league" : "Weeks unknown"}>{d.message}</EmptyState>
      </Section>
    );
  }
  const team = d.team?.name ?? "your team";
  return (
    <div className={styles.feedback} data-testid="decision-feedback">
      {d.status === "no_archive"
        ? (
          <Section title={TITLE} subtitle={<>{team} · {d.source.label} forecasts</>} actions={sources}>
            <EmptyState title="No archived recommendations for this week">
              Nothing is graded: feedback uses only inputs stored before kickoff, never numbers rebuilt afterwards.
              <ul className="reasons">
                {d.weeks.map((w) => <li key={w.week}>Week {w.week}: {w.reason ?? "No archived recommendations."}</li>)}
                {d.source.unavailable_reason && <li>{d.source.unavailable_reason}</li>}
              </ul>
            </EmptyState>
          </Section>
        )
        : <Headline d={d} sources={sources} team={team} />}
      {d.status === "ok" && d.feedback && <Lists d={d} />}
      {d.weeks.filter((w) => w.status === "graded").map((w) => <WeekCalls key={w.week} d={d} w={w} multi={d.weeks.length > 1} />)}
      <Season d={d} />
      <FeedbackMethod d={d} />
    </div>
  );
}

function SourceTabs({ d, q, providerProjections }: { d: DecisionFeedback; q: RecapQuery; providerProjections: boolean }) {
  const keys: DfSource[] = providerProjections ? ["espn", "independent"] : ["independent"];
  if (keys.length < 2) return null;
  const view = { ...q, source: d.source.key };
  return (
    <Tabs
      label="Forecast source"
      current={recapHref(view)}
      items={keys.map((k) => ({ href: recapHref(view, { source: k }), label: k === "espn" ? "ESPN" : "Ours" }))}
    />
  );
}

function Headline({ d, sources, team }: { d: DecisionFeedback; sources: ReactNode; team: string }) {
  const t = d.summary!;
  const lineups = d.weeks.map((w) => w.lineup).filter((l) => l !== null);
  const one = lineups.length === 1 ? lineups[0] : null;
  const tone = t.graded === 0 ? "info" : t.missed > t.correct ? "caution" : "positive";
  return (
    <Verdict
      tone={tone}
      eyebrow={<>{TITLE} · {team} · {d.source.label} forecasts archived before kickoff</>}
      headline={tallyText(t)}
      actions={sources}
      figures={<>
        <VerdictFigure label="Calls that worked" value={hitRateText(t)} emphasis note="Pushes and ungraded calls left out" />
        <VerdictFigure label="Points lost to missed calls" value={costRangeText(t.opportunity_cost_lower, t.opportunity_cost_upper)}
          note="Against the archive's alternative, then the best one in hindsight" />
        {one && one.archived !== null && (
          <VerdictFigure label="Archived lineup" value={pts(one.archived)} note={`Best possible ${pts(one.best)}`} />
        )}
        {one && one.yours !== null && (
          <VerdictFigure label="Your starters" value={pts(one.yours)}
            note={one.archived_minus_yours === null ? "Same slots" : `Archive ${one.archived_minus_yours >= 0 ? "+" : ""}${one.archived_minus_yours.toFixed(2)}`} />
        )}
      </>}
    >
      Each call is the lineup the stored forecasts implied, graded by the points both players scored. A miss on a close
      call is not a confident miss.
    </Verdict>
  );
}

function PlayerCell({ p }: { p: { espn_player_id: number; full_name: string; position: string | null } }) {
  return <><PlayerLink id={p.espn_player_id} name={p.full_name} /> <Pos value={p.position} /></>;
}

function Inputs({ c }: { c: DfCall }) {
  return (
    <details className={styles.inputs}>
      <summary>Inputs</summary>
      <ul>
        {[c.start, c.over].map((s) => (
          <li key={s.espn_player_id}>
            <b>{s.full_name}</b>: forecast {pts(s.forecast)} · {inputLines(s.input).join(" · ")}
          </li>
        ))}
      </ul>
    </details>
  );
}

function CallItem({ c }: { c: DfCall }) {
  return (
    <li>
      <p className={styles.callHead}>
        {c.verdict && <Badge value={VERDICT_LABEL[c.verdict]} tone={VERDICT_TONE[c.verdict]} />}{" "}
        Start <PlayerLink id={c.start.espn_player_id} name={c.start.full_name} /> over{" "}
        <PlayerLink id={c.over.espn_player_id} name={c.over.full_name} /> <span className="faint">({c.slot}, week {c.week})</span>
      </p>
      <p className={styles.callMeta}>
        Points {outcomeText(c)} · forecasts {pts(c.start.forecast)} vs {pts(c.over.forecast)}
        {c.close_call && <> · close call</>}
        {c.verdict === "missed" && <> · lost {costText(c.opportunity_cost)}</>}
      </p>
      <Inputs c={c} />
    </li>
  );
}

function WatchItem({ p }: { p: DfPlayer }) {
  return (
    <li>
      <p className={styles.callHead}><PlayerCell p={p} /> <span className="faint">week {p.week}</span></p>
      <p className={styles.callMeta}>
        Scored {pts(p.actual)} against a forecast of {pts(p.forecast)} ({p.error > 0 ? "+" : ""}{p.error.toFixed(2)})
        {p.started_in_archive ? " · in the archived lineup" : " · on the archived bench"}
      </p>
    </li>
  );
}

function Lists({ d }: { d: DecisionFeedback }) {
  const f = d.feedback!;
  const watch = [...f.watch.above_forecast, ...f.watch.below_forecast];
  return (
    <div className={styles.lists}>
      <Section title="What worked" subtitle="Calls where the started player outscored the alternative">
        {f.worked.length ? <ul className={styles.calls}>{f.worked.map((c) => <CallItem key={`${c.week}-${c.slot}-${c.start.espn_player_id}`} c={c} />)}</ul>
          : <EmptyState title="No call worked this week" />}
      </Section>
      <Section title="What changed" subtitle="Calls where the outcome went the other way, by points lost">
        {f.changed.length ? <ul className={styles.calls}>{f.changed.map((c) => <CallItem key={`${c.week}-${c.slot}-${c.start.espn_player_id}`} c={c} />)}</ul>
          : <EmptyState title="No call missed this week" />}
      </Section>
      <Section title="What to watch" subtitle="Results furthest from the stored forecasts, and what could not be graded">
        {watch.length ? <ul className={styles.calls}>{watch.map((p) => <WatchItem key={`${p.week}-${p.espn_player_id}`} p={p} />)}</ul>
          : <EmptyState title="No forecast to compare" />}
        {f.watch.notes.length > 0 && <ul className={styles.notes}>{f.watch.notes.map((n) => <li key={n}>{n}</li>)}</ul>}
      </Section>
    </div>
  );
}

function WeekCalls({ d, w, multi }: { d: DecisionFeedback; w: DfWeek; multi: boolean }) {
  const r = w.roster_input;
  return (
    <Section
      title={multi ? `Calls, week ${w.week}` : "Calls this week"}
      subtitle={r ? <>Roster snapshot {r.snapshot_id}, captured {utcText(r.captured_at)} (before the first kickoff, {utcText(r.cutoff)})</> : undefined}
      flush
      footnote={exclusionLines(w.exclusions, d.exclusion_reasons).length > 0
        ? <>Not in the decision set: {exclusionLines(w.exclusions, d.exclusion_reasons).join("; ")}.</> : undefined}
    >
      {w.calls.length === 0 ? <EmptyState title="No slot had a benched alternative" /> : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Start</th><th>Slot</th><th>Over</th><th className="num" title="Archived pregame forecasts">Forecasts</th>
                <th className="num">Points</th><th>Result</th><th className="num" title="Lower bound: against the archive's alternative. Upper: against the best alternative in hindsight.">Points lost</th>
              </tr>
            </thead>
            <tbody>
              {w.calls.map((c) => (
                <tr key={`${c.slot}-${c.start.espn_player_id}`}>
                  <td><PlayerCell p={c.start} /><Inputs c={c} /></td>
                  <td>{c.slot}</td>
                  <td><PlayerCell p={c.over} /></td>
                  <td className="num">{pts(c.start.forecast)} / {pts(c.over.forecast)}{c.close_call && <span className="faint" title="Forecast gap under 3 points"> close</span>}</td>
                  <td className="num">{c.verdict ? outcomeText(c) : <span className="missing" title={d.exclusion_reasons[c.excluded ?? ""] ?? "Not graded"}>—</span>}</td>
                  <td>{c.verdict ? <Badge value={VERDICT_LABEL[c.verdict]} tone={VERDICT_TONE[c.verdict]} /> : <span className="faint">Not graded</span>}</td>
                  <td className="num">{c.verdict ? costText(c.opportunity_cost) : <span className="missing" title="Not graded">—</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}

function SeasonRow({ r }: { r: DfSeasonWeek }) {
  if (r.status !== "graded") {
    return (
      <tr>
        <td>Week {r.week}</td>
        <td colSpan={5}><span className="faint">No archived recommendations</span></td>
      </tr>
    );
  }
  return (
    <tr>
      <td>Week {r.week}</td>
      <td className="num">{r.graded}{r.excluded > 0 && <span className="faint" title="Calls without recorded points for both players"> +{r.excluded}</span>}</td>
      <td>{r.correct}-{r.missed}{r.push > 0 && `-${r.push}`}</td>
      <td className="num">{costRangeText(r.opportunity_cost_lower, r.opportunity_cost_upper)}</td>
      <td className="num">{r.coverage === null ? <span className="missing" title="No rostered player could be checked">—</span> : `${Math.round(r.coverage * 100)}%`}</td>
      <td className="num">{r.forecast_mae === null ? <span className="missing" title="No forecast with recorded points">—</span> : `${r.forecast_mae.toFixed(2)} (${r.forecast_n})`}</td>
    </tr>
  );
}

function Season({ d }: { d: DecisionFeedback }) {
  const s = d.season_to_date;
  if (!s || s.weeks.length === 0) return null;
  const t = s.totals;
  return (
    <Section
      title="Season to date"
      subtitle={<>{d.team?.name ?? "Your team"} only · {t.weeks_graded} of {t.weeks} {plural(t.weeks, "week")} with an archive · {tallyText(t)}</>}
      flush
      footnote={<>Points lost: {costRangeText(t.opportunity_cost_lower, t.opportunity_cost_upper)}. Forecast error is the mean absolute
        difference between points and the stored forecast on your decision set{t.forecast_mae !== null && <> ({t.forecast_mae.toFixed(2)} over {t.forecast_n} {plural(t.forecast_n, "player-week")})</>}.</>}
    >
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Week</th><th className="num" title="Graded calls (+ ungraded)">Calls</th><th title="Worked-missed-push">Record</th>
              <th className="num">Points lost</th><th className="num" title="Rostered players with a stored pregame forecast">Coverage</th>
              <th className="num" title="Mean absolute forecast error (player-weeks)">Forecast error</th>
            </tr>
          </thead>
          <tbody>{s.weeks.map((r) => <SeasonRow key={r.week} r={r} />)}</tbody>
        </table>
      </div>
    </Section>
  );
}

function FeedbackMethod({ d }: { d: DecisionFeedback }) {
  const defs = d.definitions;
  return (
    <Method title="How decision feedback is graded">
      <ul className="reasons">
        <li>Inputs are only those stored before kickoff: the roster snapshot taken before the week&apos;s first game, and
          for each player the last {d.source.label} forecast stored before his own game. Nothing is rebuilt from later data.</li>
        {Object.entries(defs).map(([k, v]) => <li key={k}>{v}</li>)}
        <li>Only your own team is read. Rule {d.rule_version}.</li>
      </ul>
    </Method>
  );
}
