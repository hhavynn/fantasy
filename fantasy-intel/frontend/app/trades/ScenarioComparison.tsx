"use client";

import Link from "next/link";
import { useEffect, useId, useState, type ReactNode } from "react";
import { Delta, Notice, ProvenanceBadge, SkeletonTable, VisuallyHidden } from "@/components/ui";
import { Missing } from "@/lib/format";
import { plural } from "@/lib/plural";
import { signalsOf } from "@/lib/signal-provenance";
import { signedText } from "@/lib/trade-format";
import {
  conflictText, horizonMismatches, matchesRequest, orderedSides, savedFreshness, scenariosSrc, shortTermLabel, summaryText,
  type Scenario, type ScenarioCompareResponse, type ScenarioRow, type ScenarioSide,
} from "@/lib/trade-scenarios";
import { analyzerLink, useScenarios } from "./ScenarioStore";
import styles from "./scenarios.module.css";

type Checked = { src: string; data: ScenarioCompareResponse | null; error: string | null };

const weeksText = (a: number, b: number) => (a === b ? `week ${a}` : `weeks ${a}–${b}`);

/**
 * The saved scenarios side by side (issue #183): for both teams of each trade, the next week's, regular season's,
 * playoffs' and rest of season's change in projected starting points, all over one horizon and one data snapshot, as
 * the backend computed them. Rows are alternatives: nothing is summed across scenarios.
 */
export function ScenarioComparison({ scenarios, startWeek }: { scenarios: Scenario[]; startWeek: number | null }) {
  const ctx = useScenarios()!;
  const leagueId = ctx.leagueId!;
  const src = scenarios.length > 0 ? scenariosSrc(leagueId, scenarios, startWeek) : null;
  const [checked, setChecked] = useState<Checked | null>(null);

  useEffect(() => {
    if (!src) return;
    const ctrl = new AbortController();
    fetch(src, { signal: ctrl.signal, cache: "no-store" })
      .then(async (r) => {
        const body = await r.json().catch(() => null);
        if (!r.ok || !body || body.contract !== "trade-scenario-compare/v1" || body.scope !== leagueId) {
          setChecked({ src, data: null, error: (body && typeof body.error === "string" ? body.error : null) ?? `The comparison failed (${r.status}).` });
        } else {
          setChecked({ src, data: body as ScenarioCompareResponse, error: null });
        }
      })
      .catch((err: unknown) => {
        if ((err as { name?: string })?.name === "AbortError") return;
        setChecked({ src, data: null, error: "The scenarios could not be compared: the server did not answer." });
      });
    return () => ctrl.abort();
  }, [src, leagueId]);

  // Only the answer for exactly this list, in this order, over this horizon is shown.
  const res = checked?.src === src && matchesRequest(checked.data, leagueId, scenarios) ? checked.data : null;
  if (!src) return null;
  if (checked?.src === src && checked.error) {
    return <Notice tone="negative" title="The comparison is unavailable">{checked.error}</Notice>;
  }
  if (!res) {
    return (
      <div role="status" aria-busy="true" aria-label="Comparing scenarios" className={styles.comparison}>
        <p className={styles.muted}>Rebuilding both teams&apos; optimal lineups for every remaining week, for each scenario…</p>
        <SkeletonTable rows={Math.min(10, scenarios.length * 2)} cols={6} />
      </div>
    );
  }
  return <Comparison res={res} scenarios={scenarios} startWeek={startWeek} />;
}

function Comparison({ res, scenarios, startWeek }: { res: ScenarioCompareResponse; scenarios: Scenario[]; startWeek: number | null }) {
  const ctx = useScenarios()!;
  const ev = res.evaluation;
  const proj = res.projection_source ?? "Unavailable";
  const name = (id: number) => res.names[String(id)]?.name ?? `Player #${id}`;
  const team = (id: number | null) => (id === null ? "no roster" : res.teams.find((t) => t.team_id === id)?.name ?? `Team ${id}`);
  const last = ev.weeks[ev.weeks.length - 1];
  const lastReg = ev.last_regular_season_week;
  const regWeeks = lastReg !== null && lastReg >= ev.start_week ? weeksText(ev.start_week, lastReg) : null;
  const poWeeks = lastReg !== null && last > lastReg ? weeksText(Math.max(ev.start_week, lastReg + 1), last) : null;
  // A phase with no week in this window has no change to report: "—" with the reason, never a measured 0.
  const noSplit = "The league's playoff start is unknown, so the season is not split";
  const phases: Phases = {
    regular: lastReg === null ? noSplit : regWeeks === null ? "No regular-season weeks in this window" : null,
    playoffs: lastReg === null ? noSplit : poWeeks === null ? "No playoff weeks in this window" : null,
  };
  const mismatched = horizonMismatches(scenarios, startWeek);
  const dupes = res.scenarios.filter((r) => r.duplicate_of !== null);
  const s = res.summary;
  const captionId = useId();

  return (
    <div className={styles.comparison} aria-label="Scenario comparison" role="region">
      <div className={styles.compareHead}>
        <p className={styles.compareLine}>
          <b>{weeksText(ev.start_week, last)}</b> for every scenario · one data snapshot · projected points: <b>{proj}</b> ·{" "}
          {summaryText(s)}
        </p>
        <ProvenanceBadge signals={signalsOf(res.provenance)} />
      </div>
      {res.stale && (
        <Notice tone="caution">The server answered from this league&apos;s previous data files, so rosters and projections may be older than the latest sync.</Notice>
      )}
      {/* Data versions are opaque and compared for equality only: a mismatch says "different", never "newer". */}
      {ctx.dataVersion && res.data_version && res.data_version !== ctx.dataVersion && (
        <Notice tone={res.stale ? "caution" : "info"}>
          This comparison was read from different league data than the rest of this page, so the rosters and saved-scenario checks above
          may not match it. Reload the page to see both from the same data.
        </Notice>
      )}
      {res.conflicts.map((c) => (
        <Notice key={c.espn_player_id} tone="caution">{conflictText(c, name(c.espn_player_id))}</Notice>
      ))}
      {dupes.length > 0 && (
        <Notice tone="info">{dupes.map((d) => `Scenario ${d.index + 1} is the same trade as scenario ${d.duplicate_of! + 1}.`).join(" ")}</Notice>
      )}
      {mismatched.length > 0 && (
        <Notice tone="info">
          {plural(mismatched.length, "Scenario")} {mismatched.map((i) => i + 1).join(", ")} {mismatched.length === 1 ? "was" : "were"} saved with a
          different start week. Every row here is compared over {weeksText(ev.start_week, last)} so the numbers share one horizon.
        </Notice>
      )}
      <p className={styles.caption} id={captionId}>
        Change in projected starting points for both teams of each saved trade. Each scenario is measured alone against the current rosters:
        the rows are alternatives and never add up.
      </p>
      <div className="table-wrap">
        <table className={styles.table} aria-describedby={captionId}>
          <caption><VisuallyHidden>Saved trade scenarios compared</VisuallyHidden></caption>
          <thead>
            <tr>
              <th scope="col">Team</th>
              <th scope="col">Receives</th>
              <th scope="col" className="num">{shortTermLabel(ev)}{ev.short_term_week !== null && !ev.start_week_overridden ? <span className={styles.th2}>week {ev.short_term_week}</span> : null}</th>
              <th scope="col" className="num">Regular season{regWeeks && <span className={styles.th2}>{regWeeks}</span>}</th>
              <th scope="col" className="num">Playoffs{poWeeks && <span className={styles.th2}>{poWeeks}</span>}</th>
              <th scope="col" className="num">Rest of season<span className={styles.th2}>{weeksText(ev.start_week, last)}</span></th>
              <th scope="col">Roster</th>
            </tr>
          </thead>
          {res.scenarios.map((row) => (
            <ScenarioGroup key={row.index} row={row} saved={scenarios[row.index]} res={res} name={name} team={team} proj={proj}
              phases={phases} />
          ))}
        </table>
      </div>
      <ul className={styles.notes}>{res.notes.map((n) => <li key={n}>{n}</li>)}</ul>
    </div>
  );
}

type Phases = { regular: string | null; playoffs: string | null };

function ScenarioGroup({ row, saved, res, name, team, proj, phases }: {
  row: ScenarioRow; saved: Scenario; res: ScenarioCompareResponse; name: (id: number) => string; team: (id: number | null) => string;
  proj: string; phases: Phases;
}) {
  const ctx = useScenarios()!;
  const fresh = savedFreshness(saved.dataVersion, res.data_version);
  const sides = orderedSides(row, ctx.team);
  const tags: ReactNode[] = [<span key="shape">{row.shape}</span>];
  if (row.status === "evaluated") {
    tags.push(<span key="fresh">{fresh === "current" ? "Saved on this data" : fresh === "different" ? "Saved on different league data: re-valued on the data shown here" : "Saved on league data of unknown version: re-valued on the data shown here"}</span>);
  }
  if (row.conflicts_with.length > 0) {
    tags.push(<span key="conflict" data-tone="caution">Shares a player with {plural(row.conflicts_with.length, "scenario")} {row.conflicts_with.map((i) => i + 1).join(", ")}</span>);
  }
  return (
    <tbody className={styles.group} data-status={row.status}>
      <tr className={styles.groupHead}>
        <th scope="rowgroup" colSpan={7}>
          <span className={styles.stick}>
            <span className={styles.groupTitle}>Scenario {row.index + 1}</span>
            <span className={styles.meta}>{tags}</span>
            <Link className={styles.inlineLink} href={analyzerLink(saved, ctx.leagueId!, ctx.team)} onClick={() => ctx.setEditing(saved.id)}>
              Full evaluation in the analyzer
            </Link>
          </span>
        </th>
      </tr>
      {row.status === "evaluated" ? sides.map((side) => (
        <SideRow key={side.team_id} side={side} name={name} proj={proj} phases={phases} mine={side.team_id === ctx.team} />
      )) : (
        <tr>
          <td colSpan={7} className={styles.problem} data-tone={row.status === "stale" ? "caution" : "negative"}>
            <span className={styles.stick}>
            {row.status === "stale"
              ? <>Out of date, not re-valued: {row.moved.map((m) => `${name(m.espn_player_id)} is no longer on ${team(m.expected_team_id)} (now ${m.current_team_id === null ? "on no roster" : `on ${m.current_team_name ?? team(m.current_team_id)}`})`).join("; ")}. Open it in the analyzer to rebuild it from the current rosters.</>
              : <>{row.status === "unavailable" ? "Not evaluated" : "Not a valid trade now"}: {row.reason}</>}
            </span>
          </td>
        </tr>
      )}
    </tbody>
  );
}

function Change({ value, known, why }: { value: number | null; known?: number | null; why: string }) {
  if (value !== null) return <Delta value={value} />;
  return (
    <span className={styles.unknown}>
      <Missing why={why} />
      {known !== undefined && known !== null && <span className={styles.known}>{signedText(known)} known</span>}
    </span>
  );
}

function SideRow({ side, name, proj, phases, mine }: { side: ScenarioSide; name: (id: number) => string; proj: string; phases: Phases; mine: boolean }) {
  const incomplete = `Incomplete: a rostered player has no ${proj} projection in ${side.weeks_incomplete.length > 0 ? weeksList(side.weeks_incomplete) : "a week"}`;
  const st = side.short_term;
  return (
    <tr data-mine={mine || undefined}>
      <td className={styles.teamCell}>{side.team_name ?? `Team ${side.team_id}`}{mine && <span className={styles.you}> (this team)</span>}</td>
      <td className={styles.receives}>{side.receives.map(name).join(", ")}</td>
      <td className="num">{st ? <Change value={st.delta} known={st.known_delta} why={incomplete} /> : <Missing why="No evaluated week" />}</td>
      <td className="num">{phases.regular ? <Missing why={phases.regular} /> : <Change value={side.regular_season_delta} why={incomplete} />}</td>
      <td className="num">{phases.playoffs ? <Missing why={phases.playoffs} /> : <Change value={side.playoff_delta} why={incomplete} />}</td>
      <td className="num"><Change value={side.ros_delta} known={side.known_ros_delta} why={incomplete} /></td>
      <td className={styles.roster}>
        {side.forced_drops.length > 0 ? `Must drop ${side.forced_drops.map((d) => d.name ?? `Player #${d.espn_player_id}`).join(", ")}` : <span className={styles.muted}>No forced drop</span>}
        {side.missing_players.length > 0 && <span className={styles.caveat}>No {proj} projection for {side.missing_players.map(name).join(", ")} in some weeks</span>}
      </td>
    </tr>
  );
}

function weeksList(weeks: number[]): string {
  return weeks.length === 1 ? `week ${weeks[0]}` : `weeks ${weeks.join(", ")}`;
}
