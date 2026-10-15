import Link from "next/link";
import type { ReactNode } from "react";
import { BackendProblem } from "@/components/BackendProblem";
import { ProjectionNotice } from "@/components/ProjectionNotice";
import { EmptyState, Method, Notice, PageHeader, Tabs } from "@/components/ui";
import { api } from "@/lib/api";
import { withSource } from "@/lib/projection-source";
import { getProjectionSource } from "@/lib/projection-source.server";
import {
  availableViews, chooseView, matchupHref, parseWeekParam, statusLabel, switcherWeeks, type LineupView,
} from "@/lib/matchup";
import type { Matchup } from "@/lib/matchup-types";
import { Bench } from "./Bench";
import { Lineups, SoloLineup } from "./Lineups";
import { Scoreboard } from "./Scoreboard";
import styles from "./matchup.module.css";

export const metadata = { title: "Matchup" };

const VIEW_LABEL: Record<LineupView, string> = { projected: "Best projected", set: "Set in ESPN" };

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
  const teamParam = typeof sp.team_id === "string" && sp.team_id !== "" ? sp.team_id : null;

  if (wk.kind === "invalid" || (teamParam !== null && !/^\d{1,6}$/.test(teamParam))) {
    return (
      <Shell title="Matchup">
        <Notice tone="caution" title={wk.kind === "invalid" ? `“${wk.raw}” is not a week` : "That team id is not valid"}>
          {wk.kind === "invalid" ? "Weeks are whole numbers, like ?week=6." : "Team ids are whole numbers."}{" "}
          <Link href="/matchup">Go to this week&apos;s matchup</Link>
        </Notice>
      </Shell>
    );
  }

  const qs = new URLSearchParams();
  if (wk.kind === "ok") qs.set("week", String(wk.week));
  if (teamParam) qs.set("team_id", teamParam);
  const res = await api<Matchup>(withSource(`/api/matchup${qs.size ? `?${qs}` : ""}`, await getProjectionSource()));

  if (!res.ok) {
    if (res.kind === "http" && (res.status === 422 || res.status === 404)) {
      return (
        <Shell title="Matchup">
          <Notice tone="caution" title={res.status === 404 ? "No such team" : "That week is outside the season"}>
            {res.error}{" "}
            <Link href={matchupHref({ team_id: res.status === 422 ? teamParam : null })}>Go to {res.status === 422 ? "the current week" : "your matchup"}</Link>
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
  const keep = { team_id: teamParam };
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
        title={`Week ${m.week} matchup`}
        meta={[
          m.league.name, phase, opp ? statusLabel(m.status) : null, m.week === here ? "current week" : null,
          m.period_weeks ? `Matchup period ${m.matchup_period} spans weeks ${m.period_weeks[0]}–${m.period_weeks[m.period_weeks.length - 1]}` : null,
        ].filter(Boolean).join(" · ")}
        actions={
          <div className={styles.weekNav}>
            {prev ? <Link className="chip" href={matchupHref({ week: prev, ...keep })}>← Week {prev}</Link> : null}
            {m.week !== here ? <Link className="chip" href={matchupHref(keep)}>This week</Link> : null}
            {next ? <Link className="chip" href={matchupHref({ week: next, ...keep })}>Week {next} →</Link> : null}
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
      {opp ? <Scoreboard m={m} opponent={opp} /> : (
        <EmptyState title={m.is_bye_week ? `${m.me.name} has a bye in week ${m.week}` : `No opponent scheduled for week ${m.week}`}>
          {m.is_bye_week
            ? "Other teams play this week; yours does not, so there is no matchup to compare."
            : "The synced ESPN schedule has no pairing for this week yet. Playoff pairings appear once the regular season is decided."}
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
          opponent={opp}
          view={view}
          actions={views.length > 1 ? (
            <Tabs
              label="Lineup shown"
              current={matchupHref({ week: m.week, lineup: view, ...keep })}
              items={views.map((v) => ({ href: matchupHref({ week: m.week, lineup: v, ...keep }), label: VIEW_LABEL[v] }))}
            />
          ) : undefined}
        />
      ) : null}
      {!opp ? <SoloLineup m={m} /> : null}
      {opp && !view ? (
        <EmptyState title="No lineups for this week yet">
          ESPN has no projections or set lineups stored for week {m.week}. They appear after the next sync.
        </EmptyState>
      ) : null}

      {view ? <Bench m={m} opponent={opp} view={opp ? view : "projected"} /> : null}

      <Method title="How this page works">
        <p>
          <b>Projected lineups.</b> ESPN&apos;s weekly projections, unmodified, run through the same optimal-lineup solver used across the site: for each
          team, the best legal lineup (including FLEX) from rostered players who are not on IR or verified NFL-ineligible. It is the best lineup the
          roster could field, not necessarily the one the manager has set. Projections are kept only for weeks that have not been played.
        </p>
        <p>
          <b>Missing is not zero.</b> A player ESPN has not projected is unknown. If a lineup-relevant player has no projection the team&apos;s total is
          shown as <i>known only</i> and no margin is drawn; a projected 0 (bye, injury) is a real value.
        </p>
        <p>
          <b>Margin and share bar.</b> The margin is one team&apos;s total minus the other&apos;s, using projected totals before the games and actual
          points after. The bar shows each team&apos;s share of the two totals combined. Neither is a win probability.
        </p>
        <p>
          <b>Set lineups and actual points.</b> Set lineups come from the lineup snapshot ESPN holds. Actual points appear only once the matchup has
          started and are as of the last sync. Records are season records to date, not as of the week shown.
        </p>
      </Method>
    </div>
  );
}
