import Link from "next/link";
import { BackendProblem } from "@/components/BackendProblem";
import { Method, Notice, PageHeader, Section, Tabs } from "@/components/ui";
import { api } from "@/lib/api";
import { when } from "@/lib/format";
import { parseSort, sortRows, type SearchParams } from "@/lib/sort";
import { WAIVER_SORT_KEYS, competitionRank, pickTopClaims, trendRank, withParams } from "@/lib/waivers";
import { POSITIONS, PAGE_SIZE, RECS, REC_LABEL, VIEWS } from "./helpers";
import { TopClaims } from "./TopClaims";
import { WaiverTable } from "./WaiverTable";
import type { WaiverRow, WaiversResponse } from "./types";
import styles from "./waivers.module.css";

export const metadata = { title: "Waivers" };

/** The backend caps a response at 360 rows, which is also the most it ever evaluates. */
const FETCH_LIMIT = 360;
/** Rows read for the top-claims band: the backend ranks actionable moves first, so a short page is enough. */
const BAND_LIMIT = 12;

const first = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);

function sortValue(row: WaiverRow, key: string): number | null {
  switch (key) {
    case "score": return row.waiver_score;
    case "trend": return trendRank(row.role_trend);
    case "owned": return row.percent_change;
    case "competition": {
      const rank = competitionRank(row.league_interest);
      // The bucket orders first; the underlying interest score (0–1) breaks ties inside it.
      return rank === null ? null : rank + (row.interest_score ?? 0) / 2;
    }
    default: return null;
  }
}

export default async function WaiversPage({ searchParams }: PageProps<"/waivers">) {
  const sp = (await searchParams) as SearchParams;
  const posParam = first(sp.pos);
  const pos = posParam && (POSITIONS as readonly string[]).includes(posParam) ? posParam : "ALL";
  const viewParam = first(sp.view);
  const view = viewParam && viewParam in VIEWS ? viewParam : "BEST";
  const recParam = first(sp.rec);
  const rec = recParam && ["CLAIM", "WAIT", "IGNORE"].includes(recParam) ? recParam : "ALL";
  const offset = Math.max(0, Number.parseInt(first(sp.offset) ?? "", 10) || 0);
  const sort = parseSort(sp, WAIVER_SORT_KEYS, { key: "rank", dir: "asc" });

  const query = (v: string, limit: number) => {
    const qs = new URLSearchParams({ view: v, limit: String(limit) });
    if (pos !== "ALL") qs.set("position", pos);
    if (rec !== "ALL") qs.set("recommendation", rec);
    return `/api/waivers?${qs}`;
  };
  const bandQs = new URLSearchParams({ view: "ALL", limit: String(BAND_LIMIT) });
  if (pos !== "ALL") bandQs.set("position", pos);

  const [res, band] = await Promise.all([
    api<WaiversResponse>(query(view, FETCH_LIMIT)),
    api<WaiversResponse>(`/api/waivers?${bandQs}`),
  ]);
  if (!res.ok) {
    return (
      <div className="stack">
        <PageHeader title="Waivers" />
        <BackendProblem error={res.error} kind={res.kind} status={res.status} />
      </div>
    );
  }
  const { players, computed_at, coverage, total } = res.data;

  // Ranked order is the backend's. A sort key re-orders the whole view, then it is paged.
  const ordered = sort.key === "rank" && sort.dir === "asc"
    ? players
    : sort.key === "rank" ? [...players].reverse()
      : sortRows(players, (r) => sortValue(r, sort.key), sort.dir);
  const pageRows = ordered.slice(offset, offset + PAGE_SIZE);
  const top = pickTopClaims(band.ok ? band.data.players : []);

  const spNoOffset: SearchParams = Object.fromEntries(Object.entries(sp).filter(([k]) => k !== "offset"));
  const viewItems = Object.entries(VIEWS).map(([key, name]) => ({ href: withParams(sp, { view: key, offset: null }), label: name }));
  const currentView = withParams(sp, { view, offset: null });
  const chipHref = (updates: Record<string, string | null>) => withParams(sp, { offset: null, ...updates });

  const empty = view === "BEST"
    ? { title: "No supported actionable pickups in this view", body: "Check the watchlist for uncertain moves, or inspect all evaluated players and their constraints. Refresh ESPN and NFL data if snapshots are missing.", href: chipHref({ view: "WATCHLIST" }), linkText: "Browse stashes and watchlist" }
    : { title: "No players match this view", body: "Loosen the position or recommendation filter, or inspect all evaluated players. Refresh ESPN and NFL data if snapshots are missing.", href: chipHref({ view: "ALL", pos: null, rec: null }), linkText: "Browse all evaluated players" };

  return (
    <div className="stack">
      <PageHeader title="Waivers" meta={<>Moves for your roster · computed {when(computed_at)}</>} />

      <div className={styles.filters}>
        <Tabs label="Waiver decision views" items={viewItems} current={currentView} />
        <div className={styles.chipRows}>
          <nav className="filters" aria-label="Player positions">
            <span className={styles.chipLabel}>Position</span>
            {POSITIONS.map((p) => (
              <Link key={p} className={`chip ${pos === p ? "active" : ""}`} aria-current={pos === p ? "page" : undefined}
                href={chipHref({ pos: p === "ALL" ? null : p })}>{p === "ALL" ? "All" : p}</Link>
            ))}
          </nav>
          <nav className="filters" aria-label="Recommendation">
            <span className={styles.chipLabel}>Recommendation</span>
            {RECS.map((r) => (
              <Link key={r} className={`chip ${rec === r ? "active" : ""}`} aria-current={rec === r ? "page" : undefined}
                href={chipHref({ rec: r === "ALL" ? null : r })}>{REC_LABEL[r]}</Link>
            ))}
          </nav>
        </div>
      </div>

      {band.ok
        ? <TopClaims top={top} position={pos} />
        : <Notice tone="caution" title="Top claims are unavailable">{band.error}</Notice>}

      <Section
        title={<>{VIEWS[view]} <span className={styles.count}>{total} {total === 1 ? "player" : "players"}</span></>}
        flush={pageRows.length > 0}
        footnote={coverage
          ? <>
            {coverage.evaluated} of {coverage.eligible} eligible players in the stored pool evaluated · {coverage.excluded_by_limit} excluded by the per-position evaluation limit. {coverage.limitation}{" "}
            Historical value and xFP are PPR-based heuristics; forward ESPN projections use your league scoring. Advice reflects stored snapshots; verify transaction locks and current availability in ESPN.
          </>
          : "Decision coverage is unavailable. Run the analytics refresh to generate decision diagnostics."}
      >
        <WaiverTable rows={pageRows} sort={sort} sp={spNoOffset} empty={empty} />
      </Section>

      {/* An empty list already shows its EmptyState; no "0 results" line beneath it. */}
      {ordered.length > 0 && (
        <nav className={styles.pager} aria-label="Waiver pages">
          {offset > 0 && <Link className="chip" href={withParams(sp, { offset: offset - PAGE_SIZE > 0 ? String(offset - PAGE_SIZE) : null })}>Previous</Link>}
          <span className="sub">{`${offset + 1}–${Math.min(offset + pageRows.length, ordered.length)} of ${ordered.length}`}</span>
          {offset + pageRows.length < ordered.length && <Link className="chip" href={withParams(sp, { offset: String(offset + PAGE_SIZE) })}>Next</Link>}
        </nav>
      )}

      <Method title="How waiver advice is calculated">
        <div className={styles.method}>
          <p>
            Advice combines four separate pieces of evidence. The existing opportunity, fit and waiver scores stay as compatibility diagnostics;
            the recommendation itself does not come from the blended score.
          </p>
          <ol>
            <li><b>Player quality.</b> Historical opportunity and production, role trend, stored forward ESPN projections and context headlines. Unknown value stays unknown in a comparison.</li>
            <li><b>Roster benefit.</b> The optimal current and fully comparable future starting lineups before and after the actual add and drop. A missing projection is never zero; a supplied zero is real. Future weeks use unmodified ESPN points and known byes, with IR excluded.</li>
            <li><b>Feasibility and sacrifice.</b> Active capacity, positional caps, IR eligibility, snapshot freshness (72 hours) and starting-slot coverage. Every active single-player drop is considered; rejected choices and defensible alternatives are listed.</li>
            <li><b>Resources.</b> Free agent, waiver priority, FAAB or unknown rules. Priority in the top three is preserved for marginal gains. No precise bids and no claim-success probabilities.</li>
          </ol>
          <p>
            Thresholds are decision aids, not calibrated probabilities: a historical starter gain of at least 1 on the 0–10 value scale, a fully comparable forward gain of at least
            1 ESPN point per week, and for a stash a value of at least 6 with a same-position depth gain of at least 1 plus a real need or a rising role. Strong but unproven value stays at Monitor.
          </p>
          <p>
            Kickers and defenses are compared forward against your weakest required starter. A gain of at least 1 upcoming point is a short-term stream; gains in two of the next three matchups make a multiweek hold.
            Lines and opponent context are shown as facts and never converted into invented fantasy points.
          </p>
          <p>
            Competition is a coarse bucket from managers&apos; past adds and positional needs. It does not predict bids. Candidate coverage is the locally stored ESPN pool, up to 60 players per position
            chosen from four lanes (forward projection, depth-chart role, historical value, ownership); it is not the whole NFL. Each pickup is a separate scenario against your current roster, not a combined claim queue.
          </p>
        </div>
      </Method>
    </div>
  );
}
