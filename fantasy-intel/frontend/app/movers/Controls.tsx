import Link from "@/components/LeagueLink";
import { withLeague } from "@/lib/league-scope";
import { currentLeague } from "@/lib/league-scope.server";
import { POSITIONS, VIEW_NAME, moversHref, type MoversState, type MoverSource, type MoverView, type SnapshotRef } from "@/lib/projection-movers";
import styles from "./movers.module.css";

const VIEWS: MoverView[] = ["ros", "week"];
const SOURCES: { value: MoverSource; label: string; title: string }[] = [
  { value: "espn", label: "ESPN", title: "ESPN's stored daily projections (default)" },
  { value: "independent", label: "Ours", title: "Fantasy Intel's own projection model" },
];

/**
 * View, position and source as link chips (shareable URLs, no client state) and the comparison point as a plain
 * GET form, so everything works without JavaScript. Every control resets the selected player, because the
 * player may not exist in the new view. `sourceChoice` false (a league whose provider publishes no projections,
 * Sleeper) replaces the source chips with a note: only Fantasy Intel's forecasts exist there. On a league page the links
 * and the form stay in that league (`/l/<lg>/movers`), with or without JavaScript.
 */
export async function Controls({ state, baselines, currentId, effectiveBaseline, sourceChoice = true }: {
  state: MoversState;
  baselines: SnapshotRef[];
  currentId: string | null;
  /** The baseline the response actually used (the default when none was chosen). */
  effectiveBaseline: string | null;
  sourceChoice?: boolean;
}) {
  const options = baselines.filter((b) => b.id !== currentId);
  const action = withLeague("/movers", (await currentLeague())?.id);
  const filtered = state.position !== null || state.baseline !== null || state.view !== "ros" || (sourceChoice && state.source !== "espn");
  return (
    <div className={styles.controls}>
      <nav className="filters" aria-label="View">
        <span className={styles.chipLabel}>View</span>
        {VIEWS.map((v) => (
          <Link key={v} className={`chip ${state.view === v ? "active" : ""}`} aria-current={state.view === v ? "page" : undefined}
            href={moversHref(state, { view: v })} scroll={false} prefetch={false}>{VIEW_NAME[v]}</Link>
        ))}
      </nav>
      <nav className="filters" aria-label="Position">
        <span className={styles.chipLabel}>Position</span>
        <Link className={`chip ${state.position === null ? "active" : ""}`} aria-current={state.position === null ? "page" : undefined}
          href={moversHref(state, { position: null })} scroll={false} prefetch={false}>All</Link>
        {POSITIONS.map((p) => (
          <Link key={p} className={`chip ${state.position === p ? "active" : ""}`} aria-current={state.position === p ? "page" : undefined}
            href={moversHref(state, { position: p })} scroll={false} prefetch={false}>{p}</Link>
        ))}
      </nav>
      {sourceChoice ? (
        <nav className="filters" aria-label="Projection source">
          <span className={styles.chipLabel}>Source</span>
          {SOURCES.map((s) => (
            <Link key={s.value} className={`chip ${state.source === s.value ? "active" : ""}`} aria-current={state.source === s.value ? "page" : undefined}
              title={s.title} href={moversHref(state, { source: s.value, baseline: null })} scroll={false} prefetch={false}>{s.label}</Link>
          ))}
        </nav>
      ) : (
        <p className="filters">
          <span className={styles.chipLabel}>Source</span>
          <span className="faint">Ours · this league&apos;s provider publishes no projections to compare.</span>
        </p>
      )}
      {options.length > 1 && (
        <form className={styles.compare} action={action} method="get" aria-label="Comparison point">
          {state.source !== "espn" && <input type="hidden" name="source" value={state.source} />}
          {state.view !== "ros" && <input type="hidden" name="view" value={state.view} />}
          {state.position && <input type="hidden" name="position" value={state.position} />}
          <label className={styles.field}>
            <span className={styles.chipLabel}>Compare with</span>
            <select name="baseline" defaultValue={effectiveBaseline ?? undefined}>
              {options.map((b) => <option key={b.id} value={b.id}>{b.label}</option>)}
            </select>
          </label>
          <button type="submit">Apply</button>
        </form>
      )}
      {filtered && <Link className="chip reset" href="/movers" prefetch={false}>Reset</Link>}
    </div>
  );
}
