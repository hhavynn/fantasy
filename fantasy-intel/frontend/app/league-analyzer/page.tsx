import Link from "@/components/LeagueLink";
import { BackendProblem } from "@/components/BackendProblem";
import { ChooseTeam, TeamPerspective, UnknownTeam } from "@/components/TeamPerspective";
import { EmptyState, Method, Notice, PageHeader, Section, Verdict, VerdictFigure } from "@/components/ui";
import { api, type ApiResult } from "@/lib/api";
import { when } from "@/lib/format";
import { projectionsPhrase } from "@/lib/labels";
import {
  apiPath, errorState, knownView, ordinal, PATH, rankText, readView, signedText, viewHref, type LaErrorState, type LaTeam,
  type LaView, type LeagueAnalyzer,
} from "@/lib/league-analyzer";
import { withLeague } from "@/lib/league-scope";
import { currentLeague } from "@/lib/league-scope.server";
import { plural } from "@/lib/plural";
import { readTeamSelection, selectedTeamId, type TeamSelection } from "@/lib/team-perspective";
import { ComparePicker, CompareView, Controls, Matrix } from "./parts";
import styles from "./analyzer.module.css";

export const metadata = { title: "League analyzer" };

const QUESTION = "Where each roster is strong or thin, position by position, against the rest of the league.";

// League analyzer (issue #191): every team's projected starters by slot group, normalized as points above replacement
// per starting slot with a league-relative score and rank, plus a compare-two-teams mode. Every number comes from the
// backend's existing engines (team value, power rankings, bye planner, manager scouting); unknown stays unknown.

export default async function LeagueAnalyzerPage({ searchParams }: PageProps<"/league-analyzer">) {
  const sp = await searchParams;
  const sel = readTeamSelection(sp);
  const header = (eyebrow = "League") => <PageHeader eyebrow={eyebrow} title="League analyzer" meta={QUESTION} />;
  if (sel.kind === "invalid") {
    return <div className="stack">{header()}<UnknownTeam path={PATH} sel={sel} /></div>;
  }
  const asked = readView(sp);
  const [res, league] = await Promise.all([api<LeagueAnalyzer>(apiPath(selectedTeamId(sel), asked)), currentLeague()]);
  if (!res.ok) {
    return <div className="stack">{header()}<AnalyzerError res={res} state={errorState(res, selectedTeamId(sel), asked)} sel={sel} sp={sp} asked={asked} /></div>;
  }
  const d = res.data;
  // a position filter this league does not have (a stale or another league's link) is dropped, never half-applied
  const view = knownView(asked, d.groups);
  const focus = d.teams.find((t) => t.team_id === d.focus_team_id) ?? null;
  const action = withLeague(PATH, league?.id);
  const team = selectedTeamId(sel);
  const weeks = d.horizon.weeks;
  const span = weeks.length ? (weeks.length === 1 ? `week ${weeks[0]}` : `weeks ${weeks[0]}–${weeks[weeks.length - 1]}`) : "no weeks";

  return (
    <div className="stack">
      {header(d.week ? `League · Week ${d.week}` : "League")}
      <TeamPerspective path={PATH} sp={sp} current={d.focus_team_id} />
      <Controls d={d} team={team} view={view} />

      {d.empty ? (
        <Section>
          <EmptyState title="Nothing to compare yet" action={<Link href="/playoff-race">See the standings</Link>}>
            <p>{d.message ?? "No team has a projected lineup."}</p>
          </EmptyState>
        </Section>
      ) : (
        <>
          <Lead d={d} focus={focus} span={span} />
          <Caveats d={d} />
          <Section
            title={view.pos ? `${view.pos} strength by team` : "Position strength by team"}
            subtitle={<>{d.horizon.label} · {span} · {projectionsPhrase(d.projection_source)} · points above replacement per starting slot, league rank and a 0–100 score within each position</>}
            flush
            footnote={<>A dash means unknown, with the reason on hover: a team with an unprojected player who could start there is left out of that position&apos;s ranks, never ranked last. K and D/ST {d.kdst_streaming ? "are valued against weekly streaming" : "are held all season"}.</>}
          >
            <Matrix d={d} team={team} view={view} />
          </Section>
          {d.compare ? (
            <CompareView d={d} c={d.compare} team={team} view={view} span={span} />
          ) : (
            <Section title="Compare two teams" subtitle="Starters and bench by position, bye conflicts and positional needs, side by side.">
              <ComparePicker d={d} view={view} action={action} />
            </Section>
          )}
          <AnalyzerMethod d={d} />
        </>
      )}
    </div>
  );
}

type SP = Record<string, string | string[] | undefined>;
type Failed = Extract<ApiResult<LeagueAnalyzer>, { ok: false }>;

/** Each failure state from the route's error code (lib/league-analyzer.ts errorState), never from its wording. */
function AnalyzerError({ res, state, sel, sp, asked }: { res: Failed; state: LaErrorState; sel: TeamSelection; sp: SP; asked: LaView }) {
  const team = selectedTeamId(sel);
  const label = (id: number | null) => (id === null ? "That team" : `Team ${id}`);
  switch (state.kind) {
    case "unknown_team":
      return <UnknownTeam path={PATH} sel={sel} />;
    case "unknown_vs":
      // the focus is valid: keep it, and offer to drop only the comparison
      return (
        <Notice tone="caution" title={`${label(state.vs)} isn't a team in this league`}>
          The comparison link may be from another season or another league.{" "}
          <Link href={viewHref(team, asked, { vs: null })}>Show the matrix without a comparison</Link>.
        </Notice>
      );
    case "unknown_teams":
      return (
        <Notice tone="caution" title={`Teams ${state.team ?? "?"} and ${state.vs ?? "?"} aren't teams in this league`}>
          The link may be from another season or another league. <Link href={PATH}>Show the default team</Link>.
        </Notice>
      );
    case "same_team":
      return (
        <Notice tone="caution" title="Pick two different teams">
          A team can&apos;t be compared with itself. <Link href={viewHref(team, asked, { vs: null })}>Show the league matrix</Link>.
        </Notice>
      );
    case "no_own_team":
      return <ChooseTeam path={PATH} sp={sp} what="how it compares with another team" />;
    case "not_synced":
      return (
        <Section>
          <EmptyState title="Your league isn't synced yet" action={<Link href="/data">Check data status</Link>}>
            <p>The analyzer needs the league&apos;s rosters, projections and matchups. Run a sync, then reload this page.</p>
            <p className="faint">{res.error}</p>
          </EmptyState>
        </Section>
      );
    default:
      return <BackendProblem error={res.error} kind={res.kind} status={res.status} />;
  }
}

/** The answer first: the focus team's strongest and thinnest positions, and its power rank. */
function Lead({ d, focus, span }: { d: LeagueAnalyzer; focus: LaTeam | null; span: string }) {
  if (!focus) {
    const ranked = d.teams.filter((t) => t.power.rank !== null).length;
    return (
      <Verdict tone="info" eyebrow={`${d.horizon.label} · ${span}`} headline="Pick a team to see where its roster is strong or thin">
        {ranked} of {d.teams.length} {plural(d.teams.length, "team")} have a projected lineup. Choose one above, or compare two teams below.
      </Verdict>
    );
  }
  const best = focus.strongest[0] ?? null;
  const worst = focus.weakest[0] ?? null;
  const cellText = (slot: string | null) => {
    if (!slot) return undefined;
    const c = focus.positions[slot];
    const a = signedText(c.above_replacement);
    return [rankText(c), a !== null ? `${a} per slot over replacement` : null].filter(Boolean).join(" · ");
  };
  const who = focus.is_mine ? "Your roster" : focus.name;
  const headline = best && worst
    ? `${who} is strongest at ${best} and thinnest at ${worst}`
    : best ? `${who} is strongest at ${best}` : `${who} has no position that can be ranked yet`;
  return (
    <Verdict
      tone={best ? "info" : "caution"}
      eyebrow={`${focus.name} · ${d.horizon.label} · ${span}`}
      headline={headline}
      figures={<>
        <VerdictFigure emphasis label="Strongest" value={best ?? "—"} note={cellText(best) ?? "Nothing ranked"} />
        <VerdictFigure label="Thinnest" value={worst ?? "—"} note={cellText(worst) ?? "Nothing ranked"} />
        <VerdictFigure label="Power rank" value={focus.power.rank ? ordinal(focus.power.rank) : "—"}
          note={focus.power.rank ? `of ${d.teams.filter((t) => t.power.rank !== null).length} · rest of season` : focus.power.unranked_reason ?? "Unranked"} />
      </>}
    >
      Ranks compare the same weeks for every team; K and D/ST are left out of strongest and thinnest because every team can stream them.
    </Verdict>
  );
}

function Caveats({ d }: { d: LeagueAnalyzer }) {
  const compat = d.compatibility;
  const dims = compat ? Object.entries(compat.dimensions).filter(([, v]) => v.status !== "handled") : [];
  return (
    <>
      {d.horizon.uncertain && d.horizon.warning && <Notice tone="caution">{d.horizon.warning}</Notice>}
      {d.caveats.length > 0 && (
        <Notice tone="caution" title="Some positions are unknown or partial">
          <ul className={styles.caveatList}>
            {d.caveats.map((c) => <li key={`${c.code}-${c.text}`}>{c.text}</li>)}
          </ul>
        </Notice>
      )}
      {compat && dims.length > 0 && (
        <Notice tone="caution" title={`League compatibility: ${compat.status}`}>
          <ul className={styles.caveatList}>
            {dims.map(([k, v]) => (
              <li key={k}><strong>{k[0].toUpperCase() + k.slice(1)}</strong> ({v.status}): {v.reason}{v.action ? ` ${v.action}` : ""}</li>
            ))}
          </ul>
          <p className="faint">The same lineup engine as Power rankings. <Link href="/league-rules">League rules</Link></p>
        </Notice>
      )}
    </>
  );
}

function AnalyzerMethod({ d }: { d: LeagueAnalyzer }) {
  return (
    <Method title="How the league analyzer is calculated">
      <ul>
        {d.method.map((m) => <li key={m}>{m}</li>)}
        <li>Units: {d.units.above_replacement}; score: {d.units.score}.</li>
      </ul>
      <p className="faint">
        Source: {projectionsPhrase(d.projection_source)}, synced {when(d.freshness.projections_synced_at)}; league synced {when(d.freshness.league_synced_at)}.
        Related: <Link href="/power-rankings">Power rankings</Link>, <Link href="/my-team/value">Team value</Link>, <Link href="/managers">Managers</Link>.
      </p>
    </Method>
  );
}

