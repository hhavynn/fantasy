import Link from "@/components/LeagueLink";
import type { ReactNode } from "react";
import { BackendProblem } from "@/components/BackendProblem";
import { ChooseTeam, TeamPerspective, UnknownTeam } from "@/components/TeamPerspective";
import { ProjectionNotice } from "@/components/ProjectionNotice";
import {
  EmptyState, Glossary, Method, Notice, PageHeader, ProvenanceBadge, Tabs,
} from "@/components/ui";
import { api } from "@/lib/api";
import { providerName } from "@/lib/labels";
import { getLeagueProfile } from "@/lib/league-profile.server";
import { currentLeague } from "@/lib/league-scope.server";
import { withSource } from "@/lib/projection-source";
import { getProjectionSource } from "@/lib/projection-source.server";
import { EMPTY_RANGES } from "@/lib/projection-ranges";
import { getRanges } from "@/lib/projection-ranges.server";
import {
  availableViews, chooseView, matchupHref, parseWeekParam, statusLabel, switcherWeeks, type LineupView,
} from "@/lib/matchup";
import { signalsOf } from "@/lib/signal-provenance";
import type { Matchup } from "@/lib/matchup-types";
import { isUnknownTeam, needsDefaultTeam, readTeamSelection, withTeamId } from "@/lib/team-perspective";
import { Bench } from "./Bench";
import { Lineups, SoloLineup } from "./Lineups";
import { Scoreboard } from "./Scoreboard";
import { MatchupExport } from "./MatchupExport";
import { matchupCopyText } from "@/lib/matchup-export";
import styles from "./matchup.module.css";

export const metadata = { title: "Matchup" };

const viewLabel = (provider: string): Record<LineupView, string> => ({ projected: "Best projected", set: `Set in ${provider}` });

/** The page shell for the states where there is nothing to show but an explanation. */
function Shell({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="stack">
      <PageHeader title={title} />
      {children}
    </div>
  );
}

export default async function MatchupPage({ searchParams }: PageProps<"/matchup">) {
  const sp = await searchParams;
  const wk = parseWeekParam(sp.week);
  const sel = readTeamSelection(sp);
  const teamParam = sel.kind === "team" ? String(sel.id) : null;

  if (sel.kind === "invalid") {
    return (
      <Shell title="Matchup">
        <UnknownTeam path="/matchup" sel={sel} />
      </Shell>
    );
  }
  if (wk.kind === "invalid") {
    return (
      <Shell title="Matchup">
        <Notice tone="caution" title={`“${wk.raw}” is not a week`}>
          Weeks are whole numbers, like ?week=6.{" "}
          <Link href={matchupHref({ team: teamParam })}>Go to this week&apos;s matchup</Link>
        </Notice>
      </Shell>
    );
  }

  const qs = new URLSearchParams();
  if (wk.kind === "ok") qs.set("week", String(wk.week));
  const [source, profile] = await Promise.all([getProjectionSource(), getLeagueProfile()]);
  // The league's provider (where lineups are set and schedules come from) and whose projections the page shows by
  // default: ESPN's, or Fantasy Intel's own where the provider publishes none (Sleeper).
  const provider = providerName(profile.provider);
  const projBy = profile.providerProjections ? "ESPN" : "Fantasy Intel";
  const res = await api<Matchup>(withTeamId(withSource(`/api/matchup${qs.size ? `?${qs}` : ""}`, source), sel));

  if (!res.ok) {
    if (needsDefaultTeam(res) && sel.kind === "default" && await currentLeague()) {
      return (
        <Shell title="Matchup">
          <ChooseTeam path="/matchup" sp={sp} what="this week\'s matchup" />
        </Shell>
      );
    }
    if (isUnknownTeam(res, sel)) {
      return (
        <Shell title="Matchup">
          <UnknownTeam path="/matchup" sel={sel} detail={res.error} />
        </Shell>
      );
    }
    if (res.kind === "http" && (res.status === 422 || res.status === 404)) {
      return (
        <Shell title="Matchup">
          <Notice tone="caution" title={res.status === 404 ? "No such team" : "That week is outside the season"}>
            {res.error}{" "}
            <Link href={matchupHref({ team: res.status === 422 ? teamParam : null })}>Go to {res.status === 422 ? "the current week" : "the default matchup"}</Link>
          </Notice>
        </Shell>
      );
    }
    return (
      <Shell title="Matchup">
        <BackendProblem error={res.error} kind={res.kind} status={res.status} />
      </Shell>
    );
  }

  const m = res.data;
  const opp = m.opponent;
  const view = chooseView(sp.lineup, m);
  const views = availableViews(m);
  const keep = { team: teamParam };
  // Rows carry a range only next to our own projected number (origin "ours"), so ESPN mode never shows one.
  const ranges = source === "ours" ? await getRanges(m.week) : EMPTY_RANGES;
  const weeks = switcherWeeks(m.weeks_available, m.week);
  const prev = weeks.filter((w) => w < m.week).at(-1);
  const next = weeks.find((w) => w > m.week);
  const here = m.league.current_week;
  const phase = m.phase === "playoff" ? "Playoffs" : m.phase === "regular" ? "Regular season" : null;
  // Without an opponent the first note only repeats the empty state below, so it is left out here.
  const notes = opp ? m.notes : m.notes.slice(1);
  const incompleteSomewhere = [m.me, opp].some((s) => s && !s.projection_complete && (s.lineup.length > 0 || s.known_points !== null));

  return (
    <div className="stack">
      <PageHeader
        eyebrow={m.me.name}
        title={`Week ${m.week} matchup`}
        meta={[
          m.league.name, phase, opp ? statusLabel(m.status) : null, m.week === here ? "current week" : null,
          m.period_weeks ? `Matchup period ${m.matchup_period} spans weeks ${m.period_weeks[0]}–${m.period_weeks[m.period_weeks.length - 1]}` : null,
        ].filter(Boolean).join(" · ")}
        provenance={<ProvenanceBadge signals={signalsOf(m.provenance)} />}
        actions={
          <div className={styles.headerActions}>
          <TeamPerspective path="/matchup" sp={sp} current={m.me.team_id}>
            <div className={styles.weekNav}>
              {prev ? <Link className="chip" href={matchupHref({ week: prev, ...keep })}>← Week {prev}</Link> : null}
              {m.week !== here ? <Link className="chip" href={matchupHref(keep)}>This week</Link> : null}
              {next ? <Link className="chip" href={matchupHref({ week: next, ...keep })}>Week {next} →</Link> : null}
            </div>
          </TeamPerspective>
          <MatchupExport text={matchupCopyText(m, projBy)} />
          </div>
        }
      >
        <Tabs
          label="Week"
          current={matchupHref({ week: m.week, ...keep })}
          items={weeks.map((w) => ({ href: matchupHref({ week: w, ...keep }), label: String(w) }))}
        />
      </PageHeader>

      <ProjectionNotice detail={m.projection_source} />
      {opp ? <Scoreboard m={m} opponent={opp} projBy={projBy} /> : (
        <EmptyState title={m.is_bye_week ? `${m.me.name} has a bye in week ${m.week}` : `No opponent scheduled for week ${m.week}`}>
          {m.is_bye_week
            ? "Other teams play this week; yours does not, so there is no matchup to compare."
            : `The synced ${provider} schedule has no pairing for this week yet. Playoff pairings appear once the regular season is decided.`}
        </EmptyState>
      )}

      {notes.length > 0 ? (
        <Notice tone={incompleteSomewhere ? "caution" : "info"} title={notes.length === 1 ? "Data note" : "Data notes"}>
          <ul className={styles.notes}>{notes.map((n) => <li key={n}>{n}</li>)}</ul>
        </Notice>
      ) : null}

      {opp && view ? (
        <Lineups
          m={m}
          ranges={ranges}
          opponent={opp}
          view={view}
          provider={provider}
          projBy={projBy}
          actions={views.length > 1 ? (
            <Tabs
              label="Lineup shown"
              current={matchupHref({ week: m.week, lineup: view, ...keep })}
              items={views.map((v) => ({ href: matchupHref({ week: m.week, lineup: v, ...keep }), label: viewLabel(provider)[v] }))}
            />
          ) : undefined}
        />
      ) : null}
      {!opp ? <SoloLineup m={m} ranges={ranges} provider={provider} projBy={projBy} /> : null}
      {opp && !view ? (
        <EmptyState title="No lineups for this week yet">
          {profile.providerProjections
            ? <>ESPN has no projections or set lineups stored for week {m.week}. They appear after the next sync.</>
            : <>No projections or set lineups are stored for week {m.week}. They appear after the next sync.</>}
        </EmptyState>
      ) : null}

      {view ? <Bench m={m} opponent={opp} view={opp ? view : "projected"} ranges={ranges} provider={provider} projBy={projBy} /> : null}

      <Glossary terms={["Projected points", "Lineup points", "Bye", "Q / D / O", "—"]} providerProjections={profile.providerProjections} />
      <Method title="How this page works">
        <p>
          <b>Projected lineups.</b> {profile.providerProjections ? <>ESPN&apos;s weekly projections, unmodified,</> : <>Fantasy Intel&apos;s own weekly projections,</>} run through the same optimal-lineup solver used across the site: for each
          team, the best legal lineup (including FLEX) from rostered players who are not on IR or verified NFL-ineligible. It is the best lineup the
          roster could field, not necessarily the one the manager has set. Projections are kept only for weeks that have not been played.
        </p>
        <p>
          <b>Missing is not zero.</b> A player {projBy} has not projected is unknown. If a lineup-relevant player has no projection the team&apos;s total is
          shown as <i>known only</i> and no margin is drawn; a projected 0 (bye, injury) is a real value.
        </p>
        <p>
          <b>Margin and share bar.</b> The margin is one team&apos;s total minus the other&apos;s, using projected totals before the games and actual
          points after. The bar shows each team&apos;s share of the two totals combined. Neither is a win probability.
        </p>
        <p>
          <b>Set lineups and actual points.</b> Set lineups come from the lineup snapshot {provider} holds. Actual points appear only once the matchup has
          started and are as of the last sync. Records are season records to date, not as of the week shown.
        </p>
      </Method>
    </div>
  );
}
