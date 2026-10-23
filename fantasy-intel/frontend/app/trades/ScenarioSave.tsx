"use client";

import { useState } from "react";
import { MAX_SCENARIOS, addScenario, findTrade, replaceScenario, validSelection, type ScenarioSelection } from "@/lib/trade-scenarios";
import { newScenarioId, nowIso, useScenarios } from "./ScenarioStore";
import styles from "./scenarios.module.css";

/**
 * Save the evaluated trade (the URL selection the result below describes) to this device's scenario watchlist, or
 * update the saved scenario this tab opened. Nothing is proposed to the other manager or sent to the provider.
 */
export function ScenarioSave({ selection }: { selection: ScenarioSelection }) {
  const ctx = useScenarios();
  const [message, setMessage] = useState("");
  if (!ctx || !validSelection(selection)) return null;
  // Outside a league there is nothing to save to (the watchlist below says so); inside one, a team is needed.
  if (!ctx.scoped) {
    return ctx.leagueId ? <p className={styles.saveBar}><span className={styles.muted}>Choose your team to save trade scenarios.</span></p> : null;
  }
  if (!ctx.ready) return null;
  const list = ctx.scenarios;
  const saved = findTrade(list, selection);
  const editingIndex = list.findIndex((s) => s.id === ctx.editing);
  const editing = editingIndex >= 0 ? list[editingIndex] : null;
  const full = list.length >= MAX_SCENARIOS;
  const meta = { savedAt: nowIso(), dataVersion: ctx.dataVersion };

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
  const update = () => {
    if (!editing) return;
    const out = replaceScenario(list, editing.id, selection, meta);
    if (out.outcome === "updated") {
      ctx.save(out.scenarios);
      setMessage(`Scenario ${editingIndex + 1} now holds this trade.`);
    }
  };

  return (
    <div className={styles.saveBar} role="group" aria-label="Save this trade">
      {saved
        ? <span className={styles.savedNote}>Saved as scenario {list.indexOf(saved) + 1} on this device</span>
        : (
          <>
            <button type="button" className={styles.button} onClick={save} disabled={full}
              title={full ? `The watchlist holds ${MAX_SCENARIOS} scenarios: remove one first` : undefined}>
              Save to watchlist
            </button>
            {editing && <button type="button" className={styles.button} onClick={update}>Update scenario {editingIndex + 1}</button>}
            {full && <span className={styles.muted}>Watchlist full ({MAX_SCENARIOS} of {MAX_SCENARIOS})</span>}
          </>
        )}
      {!ctx.available && <span className={styles.muted}>This browser is not saving: the list lasts for this visit only.</span>}
      <span className={styles.muted} aria-live="polite">{message}</span>
    </div>
  );
}
