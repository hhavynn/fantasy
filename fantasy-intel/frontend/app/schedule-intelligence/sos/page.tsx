import { BackendProblem } from "@/components/BackendProblem";
import { ChooseTeam, needsTeam } from "@/components/TeamPerspective";
import { TeamLink } from "@/components/TeamLink";
import {
  EmptyState, Glossary, Method, Notice, PageHeader, ProvenanceBadge, Section, Stat, StatStrip,
} from "@/components/ui";
import { api } from "@/lib/api";
import { plural } from "@/lib/plural";
import { signalsOf } from "@/lib/signal-provenance";
import {
  CONTRACT, PATH, SORT_KEYS, VIEW_LABEL, fmtValue, parseSosState, rowName, sosApiPath, weeksText,
  type SosHeatmap, type SosState,
} from "@/lib/sos-heatmap";
import { parseSort, type SearchParams } from "@/lib/sort";
import { Controls } from "./Controls";
import { Heatmap } from "./Heatmap";
import styles from "./sos.module.css";

export const metadata = { title: "Strength of schedule" };

const TITLE = "Strength of schedule";
const EYEBROW = "Players · Research";

// Position-specific strength of schedule (issue #188): one grid per position of each remaining week's opponent defense,
// graded on that position's primary metric against a neutral baseline, for the rest of the regular season or the
// fantasy playoffs (weeks from the league's settings). Descriptive only: no projection, no points, no composite score.
// An under-sampled defense is never graded; a missing schedule or team never gets an opponent.
export default async function StrengthOfSchedulePage({ searchParams }: PageProps<"/schedule-intelligence/sos">) {
  const sp = (await searchParams) as SearchParams;
  const state = parseSosState(sp);
  const res = await api<SosHeatmap>(sosApiPath(state));
  const back = <TeamLink href="/schedule-intelligence" prefetch={false}>Schedule by player</TeamLink>;
  if (!res.ok) {
    return (
      <div className="stack">
        <PageHeader eyebrow={EYEBROW} title={TITLE} actions={back} />
        {needsTeam(res) ? <ChooseTeam path={PATH} sp={sp} what="its roster's strength of schedule" />
          : <BackendProblem error={res.error} kind={res.kind} status={res.status} />}
      </div>
    );
  }
  const d = res.data;
  if (d.contract !== CONTRACT) {
    return (
      <div className="stack">
        <PageHeader eyebrow={EYEBROW} title={TITLE} actions={back} />
        <Notice tone="caution" title="The backend sent an unexpected format">
          This page reads the {CONTRACT} response and the backend answered with {d.contract ? `“${d.contract}”` : "no version"}. Update the backend and the app together.
        </Notice>
      </div>
    );
  }
  const sort = parseSort(sp, SORT_KEYS, { key: "permissive", dir: "desc" });
  const mine = d.teams.find((t) => t.is_mine)?.team_id ?? null;
  // The analyzed team anchors trade links: the team in the URL, else yours.
  const anchor = state.team ?? mine;
  return (
    <div className="stack">
      <PageHeader
        eyebrow={EYEBROW}
        title={TITLE}
        meta={<>{d.position} · {d.horizon.label} ({weeksText(d.horizon.weeks)}) · {rowsText(d, state)} · what each opponent
          defense has <b>observed</b> allowing, not a projection</>}
        provenance={<ProvenanceBadge signals={signalsOf(d.provenance)} />}
        actions={back}
      >
        <Controls state={state} d={d} sp={sp} />
      </PageHeader>

      <Notice tone="info">{d.notes[0]}</Notice>
      <Alerts d={d} />
      <Summary d={d} />

      {d.rows.length === 0 ? (
        <Section title="Grid">
          <EmptyState title={emptyTitle(d, state)}>
            {d.query.view === "roster" ? "Pick another team or position." : "Clear the NFL team filter or pick another position."}
          </EmptyState>
        </Section>
      ) : d.horizon.weeks.length === 0 ? (
        <Section title="Grid">
          <EmptyState title={`No ${d.horizon.label.toLowerCase()} weeks to show`}>{d.horizon.reason ?? "No remaining weeks in this horizon."}</EmptyState>
        </Section>
      ) : (
        <Heatmap d={d} state={state} sp={sp} sort={sort} myTeamId={anchor} />
      )}

      <Glossary terms={["Strength of schedule", "Neutral baseline", "EPA", "Bye", "ROS", "—"]} />
      <Method title="How the grid is built">
        <p>{d.metric.label}: {d.metric.definition} <span className={styles.faintText}>[{d.metric.numerator} / {d.metric.denominator}]</span></p>
        <p>{d.label_rule}</p>
        <p>{d.baseline_rule}</p>
        <p>{d.summary_rule}</p>
        <ul className="reasons">{d.notes.slice(1).map((n, i) => <li key={i}>{n}</li>)}</ul>
        <p className={styles.faintText}>
          Horizon weeks come from the {d.horizon.source}: regular season through{" "}
          {d.horizon.regular_season_through !== null ? `week ${d.horizon.regular_season_through}` : "an unknown week"}, final scoring
          period week {d.horizon.final_scoring_period}. Only weeks that have not kicked off are shown. Built from free nflverse
          data; no third-party rankings.
        </p>
      </Method>
    </div>
  );
}

function rowsText(d: SosHeatmap, s: SosState): string {
  if (d.query.view === "player") return "one player";
  const what = d.query.view === "teams" ? plural(d.rows_total, "NFL offense")
    : d.query.view === "roster" ? `${d.team?.name ?? "roster"} ${d.position}` : `available ${d.position}`;
  const nfl = s.nfl ? ` (${s.nfl})` : "";
  return d.query.view === "teams" ? `${d.rows_total} ${what}${nfl}` : `${what}${nfl}`;
}

function emptyTitle(d: SosHeatmap, s: SosState): string {
  if (d.query.view === "roster") return `No ${d.position} on ${d.team?.name ?? "this roster"}${s.nfl ? ` from ${s.nfl}` : ""}`;
  if (d.query.view === "free_agents") return `No available ${d.position}${s.nfl ? ` from ${s.nfl}` : ""}`;
  return d.source.schedule_available ? "No NFL team matches this filter" : "No NFL schedule is loaded";
}

function Alerts({ d }: { d: SosHeatmap }) {
  return (
    <>
      {d.horizon.reason && <Notice tone={d.horizon.known ? "info" : "caution"} title={d.horizon.known ? undefined : "Horizon from incomplete league settings"}>{d.horizon.reason}</Notice>}
      {d.window.uncertain && d.window.warning && <Notice tone="caution">{d.window.warning}</Notice>}
      {!d.source.schedule_available && <Notice tone="caution">No NFL schedule is loaded, so no opponents can be assigned. Run the NFL sync.</Notice>}
      {d.source.weeks_without_schedule.length > 0 && d.source.schedule_available && (
        <Notice tone="caution">No NFL schedule is loaded for {weeksText(d.source.weeks_without_schedule)}: those weeks show no opponent rather than a guess.</Notice>
      )}
      {!d.source.available && <Notice tone="caution" title="No defensive evidence">{d.source.reason} No matchup is graded.</Notice>}
      {d.schedule.warnings.map((w) => <Notice key={w.code} tone="info">{w.message}</Notice>)}
      {d.free_agent_order && (
        <Notice tone="info">
          {d.rows_total > d.rows_shown ? `Showing ${d.rows_shown} of ${d.rows_total} available ${d.position}; filter by NFL team to see the rest. ` : ""}
          {d.free_agent_order}
        </Notice>
      )}
    </>
  );
}

function Summary({ d }: { d: SosHeatmap }) {
  const firstBaseline = d.baselines.find((b) => b.value !== null) ?? null;
  const top = d.rows.reduce<SosHeatmap["rows"][number] | null>(
    (best, r) => (r.summary.counts.permissive > 0 && (!best || r.summary.counts.permissive > best.summary.counts.permissive) ? r : best), null);
  const ungraded = d.coverage.scheduled - d.coverage.graded;
  return (
    <StatStrip compact>
      <Stat label="Graded matchups" value={`${d.coverage.graded} of ${d.coverage.scheduled}`}
        note={ungraded > 0 ? `${ungraded} not graded: too few games sampled or no data` : "every scheduled matchup in view"}
        tone={d.coverage.scheduled > 0 && d.coverage.graded === 0 ? "caution" : undefined} />
      <Stat label="Neutral baseline" value={firstBaseline ? fmtValue(firstBaseline.value, d.metric.kind) : "—"}
        note={firstBaseline ? `week ${firstBaseline.week}, pooled over ${firstBaseline.ranked_defenses} defenses` : (d.baselines[0]?.reason ?? "no week in view")} />
      <Stat label="Most permissive weeks" value={top ? rowName(top) : "—"}
        note={top ? `${top.summary.counts.permissive} of ${top.summary.graded_weeks} graded ${plural(top.summary.graded_weeks, "week")}` : "no graded permissive week in view"} />
      <Stat label="Rows" value={d.query.view === "player" ? "One player" : VIEW_LABEL[d.query.view]}
        note={`${d.rows_shown} shown${d.rows_total > d.rows_shown ? ` of ${d.rows_total}` : ""}`} />
    </StatStrip>
  );
}
