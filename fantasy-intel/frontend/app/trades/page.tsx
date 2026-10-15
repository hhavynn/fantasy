import Link from "next/link";
import { BackendProblem } from "@/components/BackendProblem";
import { Method, Notice, PageHeader } from "@/components/ui";
import { api } from "@/lib/api";
import { canEvaluate, initialSelection, selectionKey } from "@/lib/trade-builder";
import type { TradeOptionsV3, TradeResultV3 } from "@/lib/trade-types";
import { TradeBuilder } from "./TradeBuilder";
import { TradeResultView } from "./TradeResult";

export const metadata = { title: "Trade analyzer" };

// Manual Trade Analyzer. Projected points: ESPN (unmodified). Trade impact: our exact weekly
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
  const opts = await api<TradeOptionsV3>("/api/trades/options");
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
        <PageHeader title="Trade analyzer" actions={<Link href="/trades/discover">Discover trades</Link>} />
        <Notice tone="caution" title="No remaining fantasy scoring periods">
          {evaluation.warning ?? "Every scoring week has already kicked off, so there is nothing left to evaluate."}
        </Notice>
        {evaluation.start_week_reason.reason && <p className="faint">{evaluation.start_week_reason.reason}</p>}
      </div>
    );
  }
  const maxPerSide = opts.data.max_players_per_side ?? 6;
  const mine = teams.find((t) => t.is_mine)?.team_id ?? null;
  // The URL is the evaluated selection (deep links from Discovery / My Team Value, or Evaluate).
  const initial = initialSelection(teams, { ta: one(sp.ta), tb: one(sp.tb), a: ids(sp.a), b: ids(sp.b), sw: one(sp.sw) }, mine);
  const ready = canEvaluate(initial, maxPerSide);
  const evaluated = ready
    ? await api<TradeResultV3>("/api/trades/evaluate", { team_a_id: initial.ta, team_b_id: initial.tb, team_a_sends: initial.a,
      team_b_sends: initial.b, start_week: initial.sw })
    : null;

  return (
    <div className="stack">
      <PageHeader
        title="Trade analyzer"
        meta={<>Projected points: <b>ESPN</b> · Trade impact: <b>our exact lineup simulation</b> · weeks {firstWeek}–{evaluation.final_week}</>}
        actions={<Link href="/trades/discover">Discover trades</Link>}
      />
      {evaluation.uncertain && evaluation.warning && <Notice tone="caution">{evaluation.warning}</Notice>}
      <Method title="How trades are evaluated">
        <p>
          Pick up to {maxPerSide} players from each roster. Every remaining week, both teams&apos; optimal lineups are rebuilt before and after the
          trade with the league&apos;s real slots and FLEX rules; roster capacity and ESPN position limits are enforced with exact forced drops. Only
          starters count. Evaluation starts at week {firstWeek}: {evaluation.start_week_reason.reason}
        </p>
      </Method>
      <TradeBuilder teams={teams} initial={initial} evaluatedKey={evaluated ? selectionKey(initial) : null} maxPerSide={maxPerSide}
        capacity={opts.data.active_capacity} limits={opts.data.position_limits} firstWeek={firstWeek} finalWeek={evaluation.final_week}>
        {evaluated && !evaluated.ok && <BackendProblem error={evaluated.error} kind={evaluated.kind} status={evaluated.status} />}
        {evaluated?.ok && <TradeResultView r={evaluated.data} />}
      </TradeBuilder>
    </div>
  );
}
