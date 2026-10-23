import Link from "@/components/LeagueLink";
import { withLeague } from "@/lib/league-scope";
import { currentLeague } from "@/lib/league-scope.server";
import type { SearchParams } from "@/lib/sort";
import { PATH, POSITIONS, valuesHref, type ValuesState } from "@/lib/trade-values";
import styles from "./values.module.css";

/**
 * Position as link chips and the owner as a plain GET form, so both work without JavaScript and give shareable URLs.
 * On a league page the links and the form stay in that league (`/l/<lg>/trades/values`). Sorting is kept; the
 * selected player is cleared because he may not be in the new filter.
 */
export async function Controls({ state, teams, sp }: {
  state: ValuesState;
  teams: { team_id: number; name: string | null; is_mine: boolean }[];
  sp: SearchParams;
}) {
  const action = withLeague(PATH, (await currentLeague())?.id);
  const filtered = state.position !== null || state.owner !== null;
  return (
    <div className={styles.controls}>
      <nav className="filters" aria-label="Position">
        <span className={styles.chipLabel}>Position</span>
        <Link className={`chip ${state.position === null ? "active" : ""}`} aria-current={state.position === null ? "page" : undefined}
          href={valuesHref(state, { position: null }, sp)} scroll={false} prefetch={false}>All</Link>
        {POSITIONS.map((p) => (
          <Link key={p} className={`chip ${state.position === p ? "active" : ""}`} aria-current={state.position === p ? "page" : undefined}
            href={valuesHref(state, { position: p }, sp)} scroll={false} prefetch={false}>{p}</Link>
        ))}
      </nav>
      <form className={styles.owner} action={action} method="get" aria-label="Owner">
        {state.position && <input type="hidden" name="pos" value={state.position} />}
        {(["sort", "dir"] as const).map((k) => {
          const v = Array.isArray(sp[k]) ? sp[k][0] : sp[k];
          return v ? <input key={k} type="hidden" name={k} value={v} /> : null;
        })}
        <label className={styles.field}>
          <span className={styles.chipLabel}>Owner</span>
          <select name="owner" defaultValue={state.owner === null ? "" : String(state.owner)}>
            <option value="">Every team</option>
            {teams.map((t) => (
              <option key={t.team_id} value={t.team_id}>{(t.name ?? `Team ${t.team_id}`) + (t.is_mine ? " (you)" : "")}</option>
            ))}
          </select>
        </label>
        <button type="submit">Apply</button>
      </form>
      {filtered && <Link className="chip reset" href={PATH} prefetch={false}>Reset</Link>}
    </div>
  );
}
