import { BackendProblem } from "@/components/BackendProblem";
import { RulesLink } from "@/components/RulesLink";
import { TeamLink } from "@/components/TeamLink";
import { EmptyState, Glossary, Method, Notice, PageHeader, ProvenanceBadge, Section, Stat, StatStrip } from "@/components/ui";
import { api } from "@/lib/api";
import { getLeagueProfile } from "@/lib/league-profile.server";
import { plural } from "@/lib/plural";
import { signalsOf } from "@/lib/signal-provenance";
import type { SearchParams } from "@/lib/sort";
import {
  CONTRACT, changeUnavailableText, filterRows, highestInView, parseValuesState, selectedRow, sourceText, weeksText,
  type TradeValuesResponse,
} from "@/lib/trade-values";
import { Controls } from "./Controls";
import { Detail } from "./Detail";
import { ValuesTable } from "./ValuesTable";
import styles from "./values.module.css";

export const metadata = { title: "Trade values" };

const TITLE = "Trade values";
const EYEBROW = "Trades";

// Trade values board (issue #184): every rostered player's Fantasy Intel model estimate of value (projected points
// above replacement, the trade analyzer's own asset value), filterable and sortable from the URL. No trade price, no
// market or expert consensus, no acceptance estimate; a missing value or change is shown as missing, never 0.
export default async function TradeValuesPage({ searchParams }: PageProps<"/trades/values">) {
  const sp = (await searchParams) as SearchParams;
  const [res, profile] = await Promise.all([api<TradeValuesResponse>("/api/trades/values"), getLeagueProfile()]);
  if (!res.ok) {
    return (
      <div className="stack">
        <PageHeader eyebrow={EYEBROW} title={TITLE} />
        <BackendProblem error={res.error} kind={res.kind} status={res.status} />
      </div>
    );
  }
  const d = res.data;
  if (d.contract !== CONTRACT) {
    return (
      <div className="stack">
        <PageHeader eyebrow={EYEBROW} title={TITLE} />
        <Notice tone="caution" title="The backend sent an unexpected format">
          This page reads the {CONTRACT} response and the backend answered with {d.contract ? `“${d.contract}”` : "no version"}. Update the backend and the app together.
        </Notice>
      </div>
    );
  }
  const provenance = <ProvenanceBadge signals={signalsOf(d.provenance)} />;
  const analyzer = <TeamLink href="/trades">Trade analyzer</TeamLink>;
  if (!d.available || !d.basis) {
    return (
      <div className="stack">
        <PageHeader eyebrow={EYEBROW} title={TITLE} provenance={provenance} actions={analyzer} />
        <Section title="Values">
          <EmptyState title="No values for the rest of this season">{d.reason ?? "No remaining weeks to value."}</EmptyState>
        </Section>
      </div>
    );
  }

  const b = d.basis;
  const state = parseValuesState(sp);
  const rows = filterRows(d.players, state);
  const selected = selectedRow(d.players, state.player);
  const top = highestInView(rows, state.position);
  const ch = d.change;
  const projections = signalsOf(d.provenance).find((s) => s.signal === "projections") ?? null;

  return (
    <div className="stack">
      <PageHeader
        eyebrow={EYEBROW}
        title={TITLE}
        meta={<>{b.label}: projected points above replacement · <b>{sourceText(b)}</b> · {weeksText(b.window.weeks)}</>}
        provenance={provenance}
        actions={analyzer}
      >
        <Controls state={state} teams={d.teams} sp={sp} />
      </PageHeader>

      <Notice tone="info" title="Model estimates, not trade prices">
        {d.note} Values are comparable only with each other on this board. {b.cross_position}
      </Notice>
      {b.window.uncertain && b.window.warning && <Notice tone="caution">{b.window.warning}</Notice>}

      <StatStrip compact>
        <Stat label="Valued players" value={`${d.summary?.valued ?? 0} of ${d.summary?.players ?? 0}`}
          note={(d.summary?.unvalued ?? 0) > 0 ? `${d.summary!.unvalued} without a value (missing projection)` : "every rostered player"} />
        <Stat label="Highest in view" value={top ? top.value!.toFixed(1) : "—"}
          note={top ? `${top.name ?? `Player #${top.espn_player_id}`}${state.position ? "" : ", above a free agent"}` : "no comparable value in this filter"} />
        <Stat label="Sample window" value={weeksText(b.window.weeks)}
          note={`${b.window.weeks.length} ${plural(b.window.weeks.length, "week")} of projections`} />
        <Stat label="Change since" value={ch?.available && ch.baseline ? ch.baseline.label : "Unavailable"}
          note={ch?.available ? weeksText(ch.weeks) : "no earlier-week snapshot"} />
      </StatStrip>

      {!ch?.available && (
        <Notice tone="info" title="Week-over-week change unavailable">
          {changeUnavailableText(ch)} Change is shown only against a stored snapshot from an earlier week; it is never filled in as 0.
        </Notice>
      )}

      {selected && <Detail d={d} r={selected} projections={projections} />}
      {state.player !== null && !selected && (
        <Notice tone="info" title="That player is not on a roster in this league">Pick a player from the table.</Notice>
      )}
      <ValuesTable d={d} rows={rows} state={state} sp={sp} />

      <Glossary terms={["ROS", "Projected points", "FLEX", "IR", "—"]} providerProjections={profile.providerProjections} />
      <Method title="How values are calculated">
        <p>{b.calculation}</p>
        <p>{b.comparable_within}</p>
        <p>
          Source: {sourceText(b)}, calculation {b.calculation_version}. Each week of a player&apos;s calculation names the
          source or model version that produced it. {b.scoring} {b.rostered_only} {b.kdst}
        </p>
        <p>
          League starters per position used for ranks and the league-structure level:{" "}
          {Object.entries(b.league_starters).map(([pos, n]) => `${pos} ${n}`).join(", ")}.{b.flex_note ? ` ${b.flex_note}` : ""}
          {b.replacement_fallback_positions.length > 0 && ` No free agent is projected at ${b.replacement_fallback_positions.join(", ")} in some weeks, so the league-structure level stands in there; those values are ranked only within their position.`}
          {b.no_replacement_positions.length > 0 && ` At ${b.no_replacement_positions.join(", ")} some weeks have no measurable replacement level (no projected free agent and no rostered player beyond the starters), so those players have no value rather than a value above 0.`}
        </p>
        <p>{ch?.method}{ch?.notes.length ? ` ${ch.notes.join(" ")}` : ""}</p>
        <p className={styles.faintText}>
          Not a trade price, a market value or an expert consensus, and no estimate of whether a trade would be accepted. <RulesLink section="scoring">This league&apos;s scoring</RulesLink>
        </p>
      </Method>
    </div>
  );
}
