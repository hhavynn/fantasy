import { Suspense } from "react";
import { BackendProblem } from "@/components/BackendProblem";
import { RulesLink } from "@/components/RulesLink";
import { UnknownTeam } from "@/components/TeamPerspective";
import {
  Glossary, Method, Notice, PageHeader, ProvenanceBadge,
} from "@/components/ui";
import { api } from "@/lib/api";
import { parseLocks, type CounterofferResponse } from "@/lib/counteroffers";
import { projectionName, providerName } from "@/lib/labels";
import { getLeagueProfile } from "@/lib/league-profile.server";
import { currentLeague } from "@/lib/league-scope.server";
import { canEvaluate, initialSelection, selectionKey } from "@/lib/trade-builder";
import { readTeamSelection } from "@/lib/team-perspective";
import type { TradeOptionsV3, TradeResultV3 } from "@/lib/trade-types";
import { signalsOf } from "@/lib/signal-provenance";
import { CounterofferAction, Counteroffers, CounteroffersSkeleton } from "./Counteroffers";
import { EvaluatedTrade, EvaluatedTradeSkeleton } from "./EvaluatedTrade";
import { ScenarioProvider } from "./ScenarioStore";
import { ScenarioWatchlist } from "./ScenarioWatchlist";
import { TradeBuilder } from "./TradeBuilder";
import { TeamLink } from "@/components/TeamLink";

export const metadata = { title: "Trade analyzer" };

// Manual Trade Analyzer. Projected points: the league's default source (ESPN unmodified; Ours for a league whose
// provider publishes no projections). Trade impact: our exact weekly
// optimal-lineup simulation for both teams. Any player counts per side up to the configured limit.
// No trade grade, no acceptance probability. Team and player selection is client-side (TradeBuilder);
// the evaluation is rendered on the server for the URL selection only.

function ids(v: string | string[] | undefined): number[] {
  const arr = v === undefined ? [] : Array.isArray(v) ? v : [v];
  return arr.map(Number).filter((n) => Number.isFinite(n));
}

function one(v: string | string[] | undefined): number | null {
  const x = Array.isArray(v) ? v[0] : v;
  return x === undefined || x === "" || !Number.isFinite(Number(x)) ? null : Number(x);
}

export default async function TradesPage({ searchParams }: PageProps<"/trades">) {
  const sp = await searchParams;
  const [opts, profile, league] = await Promise.all([api<TradeOptionsV3>("/api/trades/options"), getLeagueProfile(), currentLeague()]);
  // Trades follow the league's default projections; the provider sets the roster rules.
  const proj = projectionName(profile.defaultSource);
  const pv = providerName(profile.provider);
  if (!opts.ok) {
    return (
      <div className="stack">
        <PageHeader title="Trade analyzer" />
        <BackendProblem error={opts.error} kind={opts.kind} status={opts.status} />
      </div>
    );
  }
  const { teams, evaluation } = opts.data;
  const firstWeek = evaluation.start_week;
  if (firstWeek === null || evaluation.remaining_weeks === false) {
    return (
      <div className="stack">
        <PageHeader title="Trade analyzer" actions={<TeamLink href="/trades/discover">Discover trades</TeamLink>} />
        <Notice tone="caution" title="No remaining fantasy scoring periods">
          {evaluation.warning ?? "Every scoring week has already kicked off, so there is nothing left to evaluate."}
        </Notice>
        {evaluation.start_week_reason.reason && <p className="faint">{evaluation.start_week_reason.reason}</p>}
      </div>
    );
  }
  const maxPerSide = opts.data.max_players_per_side ?? 6;
  // Team A defaults to the selected team (`?team=`, carried from other pages), else the owner's team.
  const sel = readTeamSelection(sp);
  if (sel.kind === "invalid" || (sel.kind === "team" && !teams.some((t) => t.team_id === sel.id))) {
    return (
      <div className="stack">
        <PageHeader title="Trade analyzer" />
        <UnknownTeam path="/trades" sel={sel} />
      </div>
    );
  }
  const mine = sel.kind === "team" ? sel.id : teams.find((t) => t.is_mine)?.team_id ?? null;
  // The URL is the evaluated selection (deep links from Discovery / My Team Value, or Evaluate).
  const initial = initialSelection(teams, { ta: one(sp.ta), tb: one(sp.tb), a: ids(sp.a), b: ids(sp.b), sw: one(sp.sw) }, mine);
  const ready = canEvaluate(initial, maxPerSide);
  // Not awaited: the lineup simulation is the slow part, so the builder renders now and the result streams in.
  // The data version the page was rendered from (signal-provenance.v1): saved scenarios record it, so a scenario saved
  // on different data is labelled and re-valued rather than shown as current.
  const dataVersion = signalsOf(opts.data.provenance).find((x) => x.signal === "league")?.snapshot_version ?? null;
  const rosterNames = teams.map((t) => ({ team_id: t.team_id, name: t.name,
    roster: t.roster.map((p) => ({ espn_player_id: p.espn_player_id, name: p.name })) }));
  const evaluated = ready
    ? api<TradeResultV3>("/api/trades/evaluate", { team_a_id: initial.ta, team_b_id: initial.tb, team_a_sends: initial.a,
      team_b_sends: initial.b, start_week: initial.sw })
    : null;
  // Counteroffers (#214) run only when asked for (`co=1`), on the evaluated offer over the default horizon, with the
  // offer players the URL keeps in the deal (`lock`). The data version makes a changed league a 409, not a stale answer.
  const selection = ready ? { ta: initial.ta!, tb: initial.tb!, a: initial.a, b: initial.b, sw: initial.sw } : null;
  const counterable = selection !== null && selection.sw === null ? selection : null;
  const locks = selection ? parseLocks(ids(sp.lock), [...selection.a, ...selection.b]) : [];
  const counter = counterable && one(sp.co) === 1
    ? api<CounterofferResponse>("/api/trades/counteroffers", { team_a_id: counterable.ta, team_b_id: counterable.tb,
      team_a_sends: counterable.a, team_b_sends: counterable.b, locked: locks, data_version: dataVersion })
    : null;

  return (
    <div className="stack">
      <PageHeader
        eyebrow="Trades"
        title="Trade analyzer"
        meta={<>Projected points: <b>{proj}</b> · Trade impact: <b>our exact lineup simulation</b> · weeks {firstWeek}–{evaluation.final_week}</>}
        provenance={<ProvenanceBadge signals={signalsOf(opts.data.provenance)} />}
        actions={<TeamLink href="/trades/discover">Discover trades</TeamLink>}
      />
      {evaluation.uncertain && evaluation.warning && <Notice tone="caution">{evaluation.warning}</Notice>}
      {/* The scenario watchlist (issue #183) lives on this device under the canonical league and the team the page is
          for; the provider is keyed by both so no state crosses a scope change. */}
      <ScenarioProvider key={`${league?.id ?? "unscoped"}:${mine ?? "none"}`} leagueId={league?.id ?? null} team={mine}
        dataVersion={dataVersion} teams={rosterNames} firstWeek={firstWeek} finalWeek={evaluation.final_week}>
        <TradeBuilder teams={teams} initial={initial} evaluatedKey={evaluated ? selectionKey(initial) : null} maxPerSide={maxPerSide}
          capacity={opts.data.active_capacity} limits={opts.data.position_limits} firstWeek={firstWeek} finalWeek={evaluation.final_week}
          projLabel={proj} providerLabel={pv}>
          {evaluated && (
            <Suspense fallback={<EvaluatedTradeSkeleton />}>
              <EvaluatedTrade result={evaluated} selection={selection!} />
            </Suspense>
          )}
          {counter && (
            <Suspense fallback={<CounteroffersSkeleton />}>
              <Counteroffers result={counter} selection={counterable!} locks={locks} />
            </Suspense>
          )}
          {counterable && !counter && <CounterofferAction selection={counterable} />}
        </TradeBuilder>
        <ScenarioWatchlist providerLabel={pv} />
      </ScenarioProvider>
      <Glossary terms={["Lineup points", "ROS", "Projected points", "FLEX", "IR", "—"]} providerProjections={profile.providerProjections} />
      <Method title="How trades are evaluated">
        <p>
          Pick up to {maxPerSide} players from each roster. Every remaining week, both teams&apos; optimal lineups are rebuilt before and after the
          trade with the league&apos;s real slots and FLEX rules; roster capacity and {pv} position limits are enforced with exact forced drops. Only
          starters count. Evaluation starts at week {firstWeek}: {evaluation.start_week_reason.reason}
        </p>
        <p>
          <RulesLink section="roster">Roster slots and position limits used here</RulesLink>
        </p>
      </Method>
    </div>
  );
}
