"use client";

import Link from "next/link";
import { useState } from "react";
import { Method, Section } from "@/components/ui";
import { plural } from "@/lib/plural";
import {
  MAX_SCENARIOS, duplicateScenario, removeScenario, rosterMoves, savedFreshness,
} from "@/lib/trade-scenarios";
import { ScenarioComparison } from "./ScenarioComparison";
import { analyzerLink, namer, newScenarioId, nowIso, useScenarios } from "./ScenarioStore";
import styles from "./scenarios.module.css";

/**
 * The scenario watchlist (issue #183): up to five saved trades, kept on this device for this league and team, each
 * reopened in the analyzer, duplicated as a starting point for a variant, removed, or compared side by side. The list
 * is checked against the page's current rosters as it is shown, and the comparison re-values every scenario on the
 * current data. Nothing is proposed or submitted.
 */
export function ScenarioWatchlist({ providerLabel }: { providerLabel: string }) {
  const ctx = useScenarios();
  const [comparing, setComparing] = useState(false);
  const [startWeek, setStartWeek] = useState<number | null>(null);
  if (!ctx) return null;
  const { scenarios, teams, leagueId, team } = ctx;
  const names = namer(teams);
  const full = scenarios.length >= MAX_SCENARIOS;

  const duplicate = (id: string) => {
    const out = duplicateScenario(scenarios, id, { id: newScenarioId(), savedAt: nowIso() });
    if (out.outcome === "added") ctx.save(out.scenarios);
  };
  const remove = (id: string) => ctx.save(removeScenario(scenarios, id));

  return (
    <Section title={<>Saved scenarios <span className={styles.count}>{scenarios.length} of {MAX_SCENARIOS}</span></>}
      subtitle={`Saved on this device for this league and team. Not synced between devices. Not submitted: nothing here is sent to ${providerLabel}.`}>
      <div className={styles.watchlist} aria-label="Trade scenario watchlist" role="region">
        {!ctx.scoped ? <p className={styles.muted}>{leagueId ? "Choose your team to save trade scenarios." : "Open a league to save trade scenarios."}</p>
          : !ctx.ready ? <p role="status" className={styles.muted}>Loading saved scenarios…</p>
            : scenarios.length === 0 ? (
              <p className={styles.muted}>No saved scenarios yet. Evaluate a trade, then save it to come back to it or compare it with up to {MAX_SCENARIOS - 1} others.</p>
            ) : (
              <>
                <ol className={styles.list} aria-label="Saved trades">
                  {scenarios.map((s, i) => {
                    const moves = rosterMoves(s, teams);
                    const fresh = savedFreshness(s.dataVersion, ctx.dataVersion);
                    return (
                      <li key={s.id} className={styles.item} data-scenario={i + 1}>
                        <span className={styles.number} aria-hidden="true">{i + 1}</span>
                        <div className={styles.what}>
                          <p className={styles.trade}>
                            <span className={styles.sideA}>{names.team(s.ta)} gives {s.a.map(names.player).join(", ")}</span>
                            <span className={styles.arrow} aria-hidden="true">⇄</span>
                            <span className={styles.sideB}>{names.team(s.tb)} gives {s.b.map(names.player).join(", ")}</span>
                          </p>
                          <p className={styles.meta}>
                            <span>{s.a.length}-for-{s.b.length}</span>
                            <span>{s.sw === null ? "from the earliest open week" : `from week ${s.sw}`}</span>
                            <span data-tone={fresh === "current" ? undefined : "caution"}>
                              {fresh === "current" ? "Saved on the current league data"
                                : fresh === "different" ? "Saved on different league data: compare to re-value"
                                  : "Saved on league data of unknown version: compare to re-value"}
                            </span>
                            {ctx.editing === s.id && <span>Open in the analyzer</span>}
                          </p>
                          {moves.length > 0 && (
                            <p className={styles.moved} data-tone="caution">
                              No longer valid as saved: {moves.map((m) => `${names.player(m.id)} is no longer on ${names.team(m.from)} (now ${m.now === null ? "on no roster" : `on ${names.team(m.now)}`})`).join("; ")}.
                              It will not be re-valued until it is rebuilt from the current rosters.
                            </p>
                          )}
                        </div>
                        <div className={styles.actions}>
                          <Link className={styles.button} href={analyzerLink(s, leagueId!, team)} onClick={() => ctx.setEditing(s.id)}
                            aria-label={`Open scenario ${i + 1} in the analyzer`}>Open</Link>
                          <button type="button" className={styles.button} onClick={() => duplicate(s.id)} disabled={full}
                            aria-label={`Duplicate scenario ${i + 1}`} title={full ? `The watchlist holds ${MAX_SCENARIOS} scenarios` : undefined}>Duplicate</button>
                          <button type="button" className={styles.button} onClick={() => remove(s.id)} aria-label={`Remove scenario ${i + 1}`}>Remove</button>
                        </div>
                      </li>
                    );
                  })}
                </ol>
                <div className={styles.compareBar}>
                  <label className={styles.week}>Compare from week
                    <select value={startWeek ?? ""} onChange={(e) => setStartWeek(e.target.value === "" ? null : Number(e.target.value))}>
                      <option value="">auto ({ctx.firstWeek})</option>
                      {Array.from({ length: Math.max(0, ctx.finalWeek - ctx.firstWeek + 1) }, (_, k) => ctx.firstWeek + k)
                        .map((w) => <option key={w} value={w}>{w}</option>)}
                    </select>
                  </label>
                  <button type="button" className={styles.primary} onClick={() => setComparing(true)} aria-pressed={comparing}>
                    {scenarios.length === 1 ? "Recheck the scenario" : `Compare ${scenarios.length} ${plural(scenarios.length, "scenario")}`}
                  </button>
                  {comparing && <button type="button" className={styles.button} onClick={() => setComparing(false)}>Hide comparison</button>}
                </div>
                {comparing && <ScenarioComparison scenarios={scenarios} startWeek={startWeek} />}
              </>
            )}
        <Method title="How saved scenarios work">
          <p>
            A scenario keeps only the two teams, the players each sends, its start week, when it was saved and the league data version it was
            saved on. No result is stored: comparing re-values every scenario now, with the analyzer&apos;s own lineup simulation, over one shared
            set of weeks and one data snapshot.
          </p>
          <p>
            Scenarios are alternatives. Each is measured alone against the current rosters, so their deltas never add up, and two scenarios that
            share a player cannot both happen as saved. A scenario whose players have moved is flagged and not re-valued.
          </p>
        </Method>
      </div>
    </Section>
  );
}
