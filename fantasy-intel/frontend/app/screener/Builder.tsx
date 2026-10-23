import Link from "@/components/LeagueLink";
import { withLeague } from "@/lib/league-scope";
import { currentLeague } from "@/lib/league-scope.server";
import {
  GROUP_NAMES, LIMITS, OPS, OP_TEXT, PATH, POSITIONS, carried, formRows, type Limits, type MetricInfo, type ScreenerState,
  type SearchParams,
} from "@/lib/screener";
import styles from "./screener.module.css";

const UNIT_SHORT = { percent: "%", per_game: "per game", points_per_game: "pts per game" } as const;

function metricOption(m: MetricInfo): string {
  const pos = m.positions.length < POSITIONS.length ? `, ${m.positions.join("/")} only` : "";
  return `${m.label} (${UNIT_SHORT[m.unit]}${pos})`;
}

/**
 * The screen as a plain GET form: every filter row is the same index of fg/fm/fo/fv, so it works without JavaScript
 * and the URL is the whole screen. On a league page the form stays in that league. Two empty rows are offered under
 * the screen's own (up to the cap); a row whose metric is left at "No filter" is ignored.
 */
export async function Builder({ state, metrics, limits, sp, summary }: {
  state: ScreenerState;
  metrics: MetricInfo[];
  limits: Limits;
  sp: SearchParams;
  summary: string;
}) {
  const action = withLeague(PATH, (await currentLeague())?.id);
  const rows = formRows(state);
  const allPos = state.pos.length === 0;
  return (
    <details className="filter-box" open>
      <summary>
        <span className="filter-box-title">Screen</span>
        <span className="filter-box-summary">{summary}</span>
      </summary>
      <form className={styles.builder} action={action} method="get" aria-label="Screen builder">
        {carried(sp).map(([k, v]) => <input key={k} type="hidden" name={k} value={v} />)}
        <fieldset className={styles.fieldset}>
          <legend className={styles.legend}>Positions</legend>
          <div className={styles.inline}>
            {POSITIONS.map((p) => (
              <label key={p} className={styles.check}>
                <input type="checkbox" name="pos" value={p} defaultChecked={allPos || state.pos.includes(p)} />
                <span>{p}</span>
              </label>
            ))}
          </div>
        </fieldset>

        <fieldset className={styles.fieldset}>
          <legend className={styles.legend}>Window and sample</legend>
          <div className={styles.grid}>
            <label className={styles.field}>
              <span className={styles.label}>First week</span>
              <input type="number" name="from" min={1} max={limits.max_week} step={1} inputMode="numeric" placeholder="1"
                defaultValue={state.from ?? ""} />
            </label>
            <label className={styles.field}>
              <span className={styles.label}>Last week</span>
              <input type="number" name="to" min={1} max={limits.max_week} step={1} inputMode="numeric" placeholder="Latest"
                defaultValue={state.to ?? ""} />
            </label>
            <label className={styles.field}>
              <span className={styles.label}>Games used</span>
              <select name="last" defaultValue={state.last === null ? "" : String(state.last)}>
                <option value="">Every game in the weeks</option>
                {Array.from({ length: limits.max_last }, (_, i) => i + 1).map((n) => (
                  <option key={n} value={n}>{n === 1 ? "Last game" : `Last ${n} games`}</option>
                ))}
              </select>
            </label>
            <label className={styles.field}>
              <span className={styles.label}>Minimum games</span>
              <input type="number" name="min_games" min={1} max={limits.max_min_games} step={1} inputMode="numeric" placeholder="1"
                defaultValue={state.minGames ?? ""} />
            </label>
            <label className={styles.field}>
              <span className={styles.label}>Minimum routes</span>
              <input type="number" name="min_routes" min={1} max={limits.max_min_routes} step={1} inputMode="numeric"
                placeholder="None" defaultValue={state.minRoutes ?? ""} aria-describedby="screener-routes-note" />
            </label>
          </div>
          <p id="screener-routes-note" className={styles.hint}>
            Minimum routes needs imported route data; a player without it is reported as undecided, never as 0 routes.
          </p>
        </fieldset>

        <fieldset className={styles.fieldset}>
          <legend className={styles.legend}>Filters</legend>
          <p className={styles.hint}>
            Values are in the metric&apos;s unit (percent for shares). Filters in the same group must all hold; a player
            matches when any group holds. At most {limits.max_filters} filters in {limits.max_groups} groups.
          </p>
          <ol className={styles.rows}>
            {rows.map((r, i) => (
              <li key={i} className={styles.row}>
                <label className={styles.field}>
                  <span className={styles.label}>Group</span>
                  <select name="fg" defaultValue={String(r?.group ?? 1)}>
                    {GROUP_NAMES.slice(0, limits.max_groups).map((g, gi) => <option key={g} value={gi + 1}>{g}</option>)}
                  </select>
                </label>
                <label className={`${styles.field} ${styles.metricField}`}>
                  <span className={styles.label}>Metric</span>
                  <select name="fm" defaultValue={r?.metric ?? ""}>
                    <option value="">No filter</option>
                    {metrics.map((m) => <option key={m.key} value={m.key}>{metricOption(m)}</option>)}
                  </select>
                </label>
                <label className={styles.field}>
                  <span className={styles.label}>Is</span>
                  <select name="fo" defaultValue={r?.op ?? "gte"}>
                    {OPS.map((o) => <option key={o} value={o}>{OP_TEXT[o]}</option>)}
                  </select>
                </label>
                <label className={styles.field}>
                  <span className={styles.label}>Value</span>
                  <input type="text" name="fv" inputMode="decimal" autoComplete="off" pattern="-?[0-9]{1,4}(\.[0-9]{1,2})?"
                    placeholder="0" defaultValue={r?.value ?? ""} className={styles.value} />
                </label>
              </li>
            ))}
          </ol>
        </fieldset>

        <div className={styles.footer}>
          <label className={styles.field}>
            <span className={styles.label}>League ownership</span>
            <select name="owner" defaultValue={state.owner}>
              <option value="any">Everyone</option>
              <option value="available">Available in this league</option>
              <option value="rostered">Rostered in this league</option>
            </select>
          </label>
          <label className={styles.field}>
            <span className={styles.label}>Rows</span>
            <select name="limit" defaultValue={String(state.limit ?? limits.default_limit)}>
              {LIMITS.filter((n) => n <= limits.max_limit).map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </label>
          <div className={styles.actions}>
            <button type="submit">Run screen</button>
            <Link className="chip reset" href={PATH} prefetch={false}>Clear</Link>
          </div>
        </div>
      </form>
    </details>
  );
}
