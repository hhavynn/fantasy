import Link from "@/components/LeagueLink";
import { withLeague } from "@/lib/league-scope";
import { currentLeague } from "@/lib/league-scope.server";
import {
  PATH, SPANS, WINDOWS, WINDOW_LABEL, mixFields, paceHref, seasonsText, type PaceLabResponse, type PaceState,
} from "@/lib/pace-lab";
import { plural } from "@/lib/plural";
import styles from "./pace-lab.module.css";

/**
 * Season, pooled seasons and window as link chips; team, opponent and the point-in-time week as a plain GET form, so
 * every filter works without JavaScript and gives a shareable URL. On a league page the links and the form stay in
 * that league.
 */
export async function Controls({ state, d }: { state: PaceState; d: PaceLabResponse }) {
  const action = withLeague(PATH, (await currentLeague())?.id);
  const q = d.query!;
  const chip = (active: boolean) => `chip ${active ? "active" : ""}`;
  const filtered = state.team !== null || state.season !== null || state.seasons !== 1 || state.window !== "season"
    || state.through !== null;
  const summary = [state.team ?? "Every offense", state.opp ? `vs ${state.opp}` : null, seasonsText(q.seasons),
    WINDOW_LABEL[state.window], q.through_week !== null ? `through week ${q.through_week}` : null].filter(Boolean).join(" · ");
  return (
    <div className={styles.controls}>
      <nav className="filters" aria-label="Season">
        <span className={styles.chipLabel}>Season</span>
        {d.available_seasons.map((s) => (
          <Link key={s} className={chip(q.season === s)} aria-current={q.season === s ? "page" : undefined}
            href={paceHref(state, { season: s, through: null })} scroll={false} prefetch={false}>{s}</Link>
        ))}
      </nav>
      <nav className="filters" aria-label="Seasons pooled">
        <span className={styles.chipLabel}>Pooled</span>
        {SPANS.map((n) => (
          <Link key={n} className={chip(state.seasons === n)} aria-current={state.seasons === n ? "page" : undefined}
            href={paceHref(state, { seasons: n })} scroll={false} prefetch={false}>{n} {plural(n, "season")}</Link>
        ))}
      </nav>
      <nav className="filters" aria-label="Games used">
        <span className={styles.chipLabel}>Games</span>
        {WINDOWS.map((w) => (
          <Link key={w} className={chip(state.window === w)} aria-current={state.window === w ? "page" : undefined}
            href={paceHref(state, { window: w })} scroll={false} prefetch={false}>{WINDOW_LABEL[w]}</Link>
        ))}
      </nav>
      <details className="filter-box">
        <summary>
          <span className="filter-box-title">Filters</span>
          <span className="filter-box-summary">{summary}</span>
        </summary>
        <form className={styles.filters} action={action} method="get" aria-label="Team filters">
          {state.season !== null && <input type="hidden" name="season" value={state.season} />}
          {state.seasons !== 1 && <input type="hidden" name="seasons" value={state.seasons} />}
          {state.window !== "season" && <input type="hidden" name="window" value={state.window} />}
          {/* A custom script mix survives a team, opponent or week change (parsePaceState drops it without a team). */}
          {mixFields(state).map(([k, v]) => <input key={k} type="hidden" name={k} value={v} />)}
          <label className={styles.field}>
            Team
            <select name="team" defaultValue={state.team ?? ""}>
              <option value="">Every offense</option>
              {(d.teams ?? []).map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </label>
          <label className={styles.field}>
            Opponent (with a team)
            <select name="opp" defaultValue={state.opp ?? ""}>
              <option value="">Every opponent</option>
              {(d.teams ?? []).map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </label>
          <label className={styles.field}>
            Through week
            <select name="through" defaultValue={state.through === null ? "" : String(state.through)}>
              <option value="">Latest stored</option>
              {(d.weeks ?? []).map((w) => <option key={w} value={w}>Week {w}</option>)}
            </select>
          </label>
          <button type="submit">Apply</button>
          {filtered && <Link className="chip reset" href={PATH} prefetch={false}>Reset</Link>}
        </form>
      </details>
    </div>
  );
}
