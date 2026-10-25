import Link from "@/components/LeagueLink";
import { withLeague } from "@/lib/league-scope";
import { currentLeague } from "@/lib/league-scope.server";
import {
  HORIZONS, HORIZON_LABEL, PATH, POSITIONS, VIEWS, VIEW_LABEL, rowName, sosHref, type SosHeatmap, type SosState,
} from "@/lib/sos-heatmap";
import type { SearchParams } from "@/lib/sort";
import styles from "./sos.module.css";

/**
 * Position, horizon and rows as link chips, and the NFL team, fantasy team and player as a plain GET form, so every
 * filter works without JavaScript and gives a shareable URL. On a league page the links and the form stay in that
 * league (`/l/<lg>/schedule-intelligence/sos`).
 */
export async function Controls({ state, d, sp }: { state: SosState; d: SosHeatmap; sp: SearchParams }) {
  const action = withLeague(PATH, (await currentLeague())?.id);
  const chip = (active: boolean) => `chip ${active ? "active" : ""}`;
  const players = d.rows.filter((r) => r.player);
  const focused = state.player !== null ? players.find((r) => r.player?.espn_player_id === state.player) : undefined;
  const summary = [focused ? rowName(focused) : VIEW_LABEL[state.view], state.nfl ?? "every NFL team",
    state.view === "roster" && d.team ? (d.team.name ?? `Team ${d.team.team_id}`) : null].filter(Boolean).join(" · ");
  const filtered = state.nfl !== null || state.player !== null || state.view !== "teams" || state.team !== null;
  return (
    <div className={styles.controls}>
      <nav className="filters" aria-label="Position">
        <span className={styles.chipLabel}>Position</span>
        {POSITIONS.map((p) => (
          <Link key={p} className={chip(d.position === p)} aria-current={d.position === p ? "page" : undefined}
            href={sosHref(state, { position: p }, sp)} scroll={false} prefetch={false}>{p}</Link>
        ))}
      </nav>
      <nav className="filters" aria-label="Horizon">
        <span className={styles.chipLabel}>Horizon</span>
        {HORIZONS.map((h) => (
          <Link key={h} className={chip(state.horizon === h)} aria-current={state.horizon === h ? "page" : undefined}
            href={sosHref(state, { horizon: h, player: state.player }, sp)} scroll={false} prefetch={false}>{HORIZON_LABEL[h]}</Link>
        ))}
      </nav>
      <details className="filter-box">
        <summary>
          <span className="filter-box-title">Filters</span>
          <span className="filter-box-summary">{summary}</span>
        </summary>
        <div className={styles.controls}>
          <nav className="filters" aria-label="Rows">
            <span className={styles.chipLabel}>Rows</span>
            {VIEWS.map((v) => (
              <Link key={v} className={chip(state.player === null && state.view === v)}
                aria-current={state.player === null && state.view === v ? "page" : undefined}
                href={sosHref(state, { view: v }, sp)} scroll={false} prefetch={false}>{VIEW_LABEL[v]}</Link>
            ))}
          </nav>
          <form className={styles.filters} action={action} method="get" aria-label="Filters">
            {state.position !== "QB" && <input type="hidden" name="pos" value={d.position} />}
            {state.horizon !== "ros" && <input type="hidden" name="horizon" value={state.horizon} />}
            {state.view !== "teams" && <input type="hidden" name="view" value={state.view} />}
            {(["sort", "dir"] as const).map((k) => {
              const v = Array.isArray(sp[k]) ? sp[k][0] : sp[k];
              return v ? <input key={k} type="hidden" name={k} value={v} /> : null;
            })}
            <label className={styles.field}>
              NFL team
              <select name="nfl" defaultValue={state.nfl ?? ""}>
                <option value="">Every team</option>
                {d.nfl_teams.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </label>
            {state.view === "roster" && (
              <label className={styles.field}>
                Fantasy team
                <select name="team" defaultValue={d.team ? String(d.team.team_id) : ""}>
                  {d.teams.map((t) => (
                    <option key={t.team_id} value={t.team_id}>{(t.name ?? `Team ${t.team_id}`) + (t.is_mine ? " (you)" : "")}</option>
                  ))}
                </select>
              </label>
            )}
            {state.view !== "roster" && state.team !== null && <input type="hidden" name="team" value={state.team} />}
            {players.length > 0 && (
              <label className={styles.field}>
                Player
                <select name="player" defaultValue={state.player === null ? "" : String(state.player)}>
                  <option value="">{state.player === null ? "Every player" : "Back to the list"}</option>
                  {players.map((r) => (
                    <option key={r.player!.espn_player_id} value={r.player!.espn_player_id}>{rowName(r)}</option>
                  ))}
                </select>
              </label>
            )}
            <button type="submit">Apply</button>
            {filtered && <Link className="chip reset" href={PATH} prefetch={false}>Reset</Link>}
          </form>
        </div>
      </details>
    </div>
  );
}
