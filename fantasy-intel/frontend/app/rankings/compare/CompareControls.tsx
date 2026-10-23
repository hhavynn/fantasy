import Link from "@/components/LeagueLink";
import { Tabs } from "@/components/ui";
import { withLeague } from "@/lib/league-scope";
import { currentLeague } from "@/lib/league-scope.server";
import {
  COMPARE_POSITIONS, SCOPE_LABEL, SHOW_LABEL, compareHref, type CompareFilters,
} from "@/lib/projection-compare";
import type { SearchParams } from "@/lib/sort";
import styles from "./compare.module.css";

const HORIZONS = [{ value: "week", label: "This week" }, { value: "ros", label: "Rest of season" }] as const;
const SCOPES = (["position", "overall"] as const).map((value) => ({ value, label: SCOPE_LABEL[value] }));
const SHOWS = (["all", "large", "compared", "missing"] as const).map((value) => ({ value, label: SHOW_LABEL[value] }));
const STATUSES = [{ value: "all", label: "All" }, { value: "rostered", label: "Rostered" }, { value: "available", label: "Available" }] as const;

/** Params the search form carries as hidden inputs: everything except the search itself and the page. */
function carried(sp: SearchParams): [string, string][] {
  const out: [string, string][] = [];
  for (const [k, v] of Object.entries(sp)) {
    if (k === "q" || k === "offset" || v === undefined) continue;
    for (const item of Array.isArray(v) ? v : [v]) out.push([k, item]);
  }
  return out;
}

function Chips({ label, name, options, current, sp, param }: {
  label: string; name: string; options: readonly { value: string; label: string }[]; current: string; sp: SearchParams; param: string;
}) {
  return (
    <nav className="filters" aria-label={label}>
      <span className={styles.chipLabel} aria-hidden="true">{name}</span>
      {options.map((o) => (
        <Link key={o.value} className={`chip ${current === o.value ? "active" : ""}`} aria-current={current === o.value ? "page" : undefined}
          href={compareHref(sp, { [param]: o.value })} scroll={false}>{o.label}</Link>
      ))}
    </nav>
  );
}

/**
 * Position tabs, window / rank scope / show / roster chips and the name search. Every control is a link or a GET form,
 * so the whole view lives in the URL (back, forward and sharing work), and on a league page it stays in that league.
 */
export async function CompareControls({ sp, filters, leagueAvailable }: { sp: SearchParams; filters: CompareFilters; leagueAvailable: boolean }) {
  const posHref = (p: string) => compareHref(sp, { pos: p === "ALL" ? null : p });
  const action = withLeague("/rankings/compare", (await currentLeague())?.id);
  const hasFilters = filters.pos !== "ALL" || filters.status !== "all" || filters.q !== "" || filters.show !== "all";
  const summary = [
    HORIZONS.find((h) => h.value === filters.horizon)?.label, SCOPE_LABEL[filters.scope], SHOW_LABEL[filters.show],
    leagueAvailable && filters.status !== "all" ? STATUSES.find((x) => x.value === filters.status)?.label : null,
  ].filter(Boolean).join(" · ");
  return (
    <div className={styles.controls}>
      <Tabs label="Position" items={COMPARE_POSITIONS.map((p) => ({ href: posHref(p), label: p === "ALL" ? "All" : p }))}
        current={posHref(filters.pos)} />
      <div className={styles.controlRow}>
        <details className={`filter-box ${styles.filterBox}`}>
          <summary>
            <span className="filter-box-title">Filters</span>
            <span className="filter-box-summary">{summary}</span>
          </summary>
          <div className={styles.chipRows}>
            <Chips label="Time window" name="Window" options={HORIZONS} current={filters.horizon} sp={sp} param="horizon" />
            <Chips label="Rank scope" name="Ranks" options={SCOPES} current={filters.scope} sp={sp} param="scope" />
            <Chips label="Which players" name="Show" options={SHOWS} current={filters.show} sp={sp} param="show" />
            {leagueAvailable && <Chips label="Roster status" name="Roster" options={STATUSES} current={filters.status} sp={sp} param="status" />}
          </div>
        </details>
        <form className={styles.search} action={action} role="search">
          {carried(sp).map(([k, v], i) => <input key={`${k}-${i}`} type="hidden" name={k} value={v} />)}
          <label className={styles.searchLabel} htmlFor="compare-q">Search</label>
          <input id="compare-q" className={styles.input} type="search" name="q" defaultValue={filters.q} maxLength={60}
            placeholder="Player name" autoComplete="off" />
          <button className={styles.submit} type="submit">Search</button>
        </form>
      </div>
      {hasFilters && (
        <div className={styles.chipTail}>
          <Link className="chip reset" href={compareHref(sp, { pos: null, status: null, q: null, show: null })} scroll={false}>Clear filters</Link>
        </div>
      )}
    </div>
  );
}
