"use client";

import { useState, useSyncExternalStore } from "react";
import { MAX_SCENARIOS, addScenario, replaceScenario, saveState, validSelection, type ScenarioSelection } from "@/lib/trade-scenarios";
import { newScenarioId, nowIso, useInPreviousResult, useScenarios } from "./ScenarioStore";
import styles from "./scenarios.module.css";

/**
 * Save the evaluated trade to this device's scenario watchlist, or update the saved scenario this tab opened (or the
 * saved copy of this trade, when only its start week changed). Rendered by EvaluatedTrade only once the evaluation has
 * succeeded, so a trade is never saved while its result is loading or after it failed, and never inside the builder's
 * "previous result" panel. Nothing is proposed to the other manager or sent to the provider.
 */
const noSubscribe = () => () => {};

export function ScenarioSave({ selection }: { selection: ScenarioSelection }) {
  const ctx = useScenarios();
  const [message, setMessage] = useState("");
  // This bar streams in later than the provider (inside the result's Suspense boundary), when the device list has
  // usually loaded already; it renders nothing until it has hydrated, so the hydrated HTML matches the server's.
  const hydrated = useSyncExternalStore(noSubscribe, () => true, () => false);
  // The builder's "previous result" panel shows an older trade than the one selected: no save offered there.
  const previous = useInPreviousResult();
  if (!hydrated || previous || !ctx || !validSelection(selection)) return null;
  // Outside a league there is nothing to save to (the watchlist below says so); inside one, a team is needed.
  if (!ctx.scoped) {
    return ctx.leagueId ? <p className={styles.saveBar}><span className={styles.muted}>Choose your team to save trade scenarios.</span></p> : null;
  }
  if (!ctx.ready) return null;
  const list = ctx.scenarios;
  const state = saveState(list, selection, ctx.editing);
  const full = list.length >= MAX_SCENARIOS;
  const meta = { savedAt: nowIso(), dataVersion: ctx.dataVersion };
  const fromWeek = (w: number | null) => (w === null ? "the earliest open week" : `week ${w}`);

  const save = () => {
    const out = addScenario(list, selection, { id: newScenarioId(), ...meta });
    if (out.outcome === "added") {
      ctx.save(out.scenarios);
      ctx.setEditing(out.id);
      setMessage(`Saved as scenario ${out.scenarios.length}.`);
    } else if (out.outcome === "full") {
      setMessage(`The watchlist holds ${MAX_SCENARIOS} scenarios: remove one to save this trade.`);
    }
  };
  const update = (id: string, index: number, what: string) => {
    const out = replaceScenario(list, id, selection, meta);
    if (out.outcome === "updated") {
      ctx.save(out.scenarios);
      ctx.setEditing(id);
      setMessage(`Scenario ${index + 1} now holds ${what}.`);
    }
  };

  return (
    <div className={styles.saveBar} role="group" aria-label="Save this trade">
      {state.kind === "saved" && <span className={styles.savedNote}>Saved as scenario {state.index + 1} on this device</span>}
      {state.kind === "start_week" && (
        <>
          <span className={styles.muted}>Saved as scenario {state.index + 1} from {fromWeek(state.savedWeek)}.</span>
          <button type="button" className={styles.button}
            onClick={() => update(state.id, state.index, `this trade from ${fromWeek(selection.sw)}`)}>
            Update scenario {state.index + 1} to start from {fromWeek(selection.sw)}
          </button>
        </>
      )}
      {state.kind === "new" && (
        <>
          <button type="button" className={styles.button} onClick={save} disabled={full}
            title={full ? `The watchlist holds ${MAX_SCENARIOS} scenarios: remove one first` : undefined}>
            Save to watchlist
          </button>
          {state.editing && (
            <button type="button" className={styles.button} onClick={() => update(state.editing!.id, state.editing!.index, "this trade")}>
              Update scenario {state.editing.index + 1}
            </button>
          )}
          {full && <span className={styles.muted}>Watchlist full ({MAX_SCENARIOS} of {MAX_SCENARIOS})</span>}
        </>
      )}
      {!ctx.available && <span className={styles.muted}>This browser is not saving: the list lasts for this visit only.</span>}
      <span className={styles.muted} aria-live="polite">{message}</span>
    </div>
  );
}
