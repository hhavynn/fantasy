"use client";

import { useActionState, useId } from "react";
import { Notice } from "@/components/ui";
import { teamOptionText, type TeamOption } from "@/lib/leagues";
import { setMyTeam } from "./actions";
import { IDLE } from "./state";
import styles from "./leagues.module.css";

/** Choose which team is "mine" in one connected league. Options come from the backend's team list. */
export function TeamPicker({ leagueId, teams, selected }: { leagueId: string; teams: TeamOption[]; selected: string | null }) {
  const [state, action, pending] = useActionState(setMyTeam.bind(null, leagueId), IDLE);
  const id = useId();
  const known = selected !== null && teams.some((t) => t.id === selected);
  return (
    <form action={action} className={styles.teamForm}>
      <div className={styles.field}>
        <label htmlFor={id}>My team</label>
        <select id={id} name="external_team_id" defaultValue={known ? selected : ""} required disabled={pending}>
          <option value="" disabled>Choose your team</option>
          {teams.map((t) => <option key={t.id} value={t.id}>{teamOptionText(t)}</option>)}
        </select>
      </div>
      <button type="submit" disabled={pending}>{pending ? "Saving…" : "Save my team"}</button>
      {state.status === "error" && state.message ? <Notice tone="negative" title="Could not save your team">{state.message}</Notice> : null}
      {state.status === "ok" && state.message ? <p className={styles.ok} role="status">{state.message}</p> : null}
    </form>
  );
}
