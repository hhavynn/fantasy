import Link from "next/link";
import { Tabs } from "@/components/ui";
import { rankingsHref, RANK_POSITIONS, type RankingsFilters } from "@/lib/rankings";
import type { SearchParams } from "@/lib/sort";
import styles from "./rankings.module.css";

const HORIZONS = [{ value: "week", label: "This week" }, { value: "ros", label: "Rest of season" }] as const;
const SOURCES = [{ value: "espn", label: "ESPN" }, { value: "ours", label: "Ours" }] as const;
const STATUSES = [{ value: "all", label: "All" }, { value: "rostered", label: "Rostered" }, { value: "available", label: "Available" }] as const;

/** Params the search form carries over as hidden inputs: everything except the search itself and the page. */
function carried(sp: SearchParams): [string, string][] {
  const out: [string, string][] = [];
  for (const [k, v] of Object.entries(sp)) {
    if (k === "q" || k === "offset" || v === undefined) continue;
    for (const item of Array.isArray(v) ? v : [v]) out.push([k, item]);
  }
  return out;
}

function Chips<T extends string>({ label, name, options, current, sp, param }: {
  label: string;
  name: string;
  options: readonly { value: T; label: string }[];
  current: T;
  sp: SearchParams;
  param: string;
}) {
  return (
    <nav className="filters" aria-label={label}>
      <span className={styles.chipLabel} aria-hidden="true">{name}</span>
      {options.map((o) => (
        <Link key={o.value} className={`chip ${current === o.value ? "active" : ""}`} aria-current={current === o.value ? "page" : undefined}
          href={rankingsHref(sp, { [param]: o.value })} scroll={false}>{o.label}</Link>
      ))}
    </nav>
  );
}

/**
 * Position tabs, horizon / source / roster-status chips and the name search. Every control is a link or a
 * GET form, so the view lives in the URL (back, forward and sharing work). The roster filter needs the
 * league's rosters, so it appears only when the backend says the league context is available.
 */
export function RankingsControls({ sp, filters, leagueAvailable }: { sp: SearchParams; filters: RankingsFilters; leagueAvailable: boolean }) {
  const posHref = (p: string) => rankingsHref(sp, { pos: p === "ALL" ? null : p });
  const hidden = carried(sp);
  const hasFilters = filters.pos !== "ALL" || filters.status !== "all" || filters.q !== "";
  return (
    <div className={styles.controls}>
      <Tabs label="Position"
        items={RANK_POSITIONS.map((p) => ({ href: posHref(p), label: p === "ALL" ? "All" : p }))}
        current={posHref(filters.pos)} />
      <div className={styles.chipRows}>
        <Chips label="Time window" name="Window" options={HORIZONS} current={filters.horizon} sp={sp} param="horizon" />
        <Chips label="Projection source" name="Projections" options={SOURCES} current={filters.source} sp={sp} param="source" />
        {leagueAvailable && <Chips label="Roster status" name="Roster" options={STATUSES} current={filters.status} sp={sp} param="status" />}
        <form className={styles.search} action="/rankings" role="search">
          {hidden.map(([k, v], i) => <input key={`${k}-${i}`} type="hidden" name={k} value={v} />)}
          <label className={styles.searchLabel} htmlFor="rankings-q">Search</label>
          <input id="rankings-q" className={styles.input} type="search" name="q" defaultValue={filters.q} maxLength={60}
            placeholder="Player name" autoComplete="off" />
          <button className={styles.submit} type="submit">Search</button>
        </form>
        {hasFilters && <Link className="chip reset" href={rankingsHref(sp, { pos: null, status: null, q: null })} scroll={false}>Clear filters</Link>}
      </div>
    </div>
  );
}
