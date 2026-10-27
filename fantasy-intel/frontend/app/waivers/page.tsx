import { Suspense } from "react";
import { BackendProblem } from "@/components/BackendProblem";
import Link from "@/components/LeagueLink";
import { SavedViews } from "@/components/SavedViews";
import { RulesLink } from "@/components/RulesLink";
import { TeamPerspective, UnknownTeam } from "@/components/TeamPerspective";
import {
  Glossary, Method, Notice, PageHeader, ProvenanceBadge, Section, Tabs,
} from "@/components/ui";
import { currentLeague } from "@/lib/league-scope.server";
import { ShortlistProvider } from "./Shortlist";
import { WaiverShortlist } from "./ClaimPlanner";
import { api } from "@/lib/api";
import { when } from "@/lib/format";
import { OWNERSHIP_UNAVAILABLE, ownershipAvailable, projectionName, providerName } from "@/lib/labels";
import { getLeagueProfile } from "@/lib/league-profile.server";
import { parseSort, sortRows, type SearchParams } from "@/lib/sort";
import { signalsOf } from "@/lib/signal-provenance";
import { isUnknownTeam, readTeamSelection, selectedTeamId, withTeamId } from "@/lib/team-perspective";
import { WAIVER_SORT_KEYS, competitionRank, knownWaiverScore, pickTopClaims, trendRank, withParams } from "@/lib/waivers";
import { POSITIONS, PAGE_SIZE, RECS, REC_LABEL, VIEWS } from "./helpers";
import { addDropHref, readAddDrop } from "@/lib/add-drop";
import { AddDropPanel, AddDropSkeleton } from "./AddDropPanel";
import adStyles from "./add-drop.module.css";
import { TopClaims } from "./TopClaims";
import { StashPanel } from "./StashPanel";
import { readStashHorizon } from "@/lib/stash-horizon";
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
    case "score": return knownWaiverScore(row);   // a partial score has no rank (sorts with the unknowns)
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
  const sel = readTeamSelection(sp);
  if (sel.kind === "invalid") {
    return (
      <div className="stack">
        <PageHeader title="Waivers" actions={<TeamPerspective path="/waivers" sp={sp} current={null} />} />
        <UnknownTeam path="/waivers" sel={sel} />
      </div>
    );
  }
  const posParam = first(sp.pos);
  const pos = posParam && (POSITIONS as readonly string[]).includes(posParam) ? posParam : "ALL";
  const viewParam = first(sp.view);
  const view = viewParam && viewParam in VIEWS ? viewParam : "BEST";
  const recParam = first(sp.rec);
  const rec = recParam && ["CLAIM", "WAIT", "IGNORE"].includes(recParam) ? recParam : "ALL";
  const offset = Math.max(0, Number.parseInt(first(sp.offset) ?? "", 10) || 0);
  const addDrop = readAddDrop(sp);
  // A link from the bye planner names the week it was planning for; waiver results are always for the current period.
  const plannedWeek = /^\d{1,2}$/.test(first(sp.week) ?? "") ? Number(first(sp.week)) : null;
  const profile = await getLeagueProfile();
  const pv = providerName(profile.provider);
  // Waivers follow the league's default projections (ESPN's; Ours where the provider publishes none).
  const proj = projectionName(profile.defaultSource);
  // Ownership % comes from ESPN only: a Sleeper league has none to show or sort by.
  const ownership = ownershipAvailable(profile.provider);
  const parsedSort = parseSort(sp, WAIVER_SORT_KEYS, { key: "rank", dir: "asc" });
  const sort = !ownership && parsedSort.key === "owned" ? { key: "rank", dir: "asc" as const } : parsedSort;

  const query = (v: string, limit: number) => {
    const qs = new URLSearchParams({ view: v, limit: String(limit) });
    if (pos !== "ALL") qs.set("position", pos);
    if (rec !== "ALL") qs.set("recommendation", rec);
    return withTeamId(`/api/waivers?${qs}`, sel);
  };
  const bandQs = new URLSearchParams({ view: "ALL", limit: String(BAND_LIMIT) });
  if (pos !== "ALL") bandQs.set("position", pos);

  const [res, band] = await Promise.all([
    api<WaiversResponse>(query(view, FETCH_LIMIT)),
    api<WaiversResponse>(withTeamId(`/api/waivers?${bandQs}`, sel)),
  ]);
  if (isUnknownTeam(res, sel)) {
    return (
      <div className="stack">
        <PageHeader title="Waivers" actions={<TeamPerspective path="/waivers" sp={sp} current={null} />} />
        <UnknownTeam path="/waivers" sel={sel} detail={res.error} />
      </div>
    );
  }
  if (!res.ok) {
    return (
      <div className="stack">
        <PageHeader title="Waivers" actions={<TeamPerspective path="/waivers" sp={sp} current={selectedTeamId(sel)} />} />
        <BackendProblem error={res.error} kind={res.kind} status={res.status} />
      </div>
    );
  }
  const league = await currentLeague();
  const { players, computed_at, coverage, total, perspective } = res.data;

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
    ? { title: "No supported actionable pickups in this view", body: `Check the watchlist for uncertain moves, or inspect all evaluated players and their constraints. Refresh ${pv} and NFL data if snapshots are missing.`, href: chipHref({ view: "WATCHLIST" }), linkText: "Browse stashes and watchlist" }
    : { title: "No players match this view", body: `Loosen the position or recommendation filter, or inspect all evaluated players. Refresh ${pv} and NFL data if snapshots are missing.`, href: chipHref({ view: "ALL", pos: null, rec: null }), linkText: "Browse all evaluated players" };

  return (
    <ShortlistProvider key={league?.id ?? "unscoped"} leagueId={league?.id ?? null}>
    <div className="stack">
      <PageHeader
        eyebrow="Waivers"
        title="Waivers"
        meta={<>Moves for {perspective ? <b>{perspective.name}</b> : "the default team"} · computed {when(computed_at)}</>}
        provenance={<ProvenanceBadge signals={signalsOf(res.data.provenance)} />}
        actions={<TeamPerspective path="/waivers" sp={sp} current={perspective?.team_id ?? null} />}
      />
      {plannedWeek !== null && (
        <Notice tone="info" title={`Looking for week ${plannedWeek} cover`}>
          These recommendations are for the current waiver period, not ranked for week {plannedWeek}. Check that a
          player&apos;s NFL team plays in week {plannedWeek} before claiming him for it.
        </Notice>
      )}
      {res.data.roster_newer_than_analysis && (
        <Notice tone="caution" title="This roster changed after the waiver analysis ran">
          {pv} rosters were re-synced after these suggestions were computed, so a suggested drop may already be gone and a fit may have changed.
          Rerun the analytics refresh before acting.
        </Notice>
      )}

      <div className={adStyles.decisions}>
        {band.ok
          ? <TopClaims top={top} position={pos} provider={pv} ownership={ownership}
            simulateHref={(row) => addDropHref("", sp, {
              add: row.espn_player_id,
              drop: row.drop_on_roster === false ? null : row.components.decision?.roster_move.drop?.espn_player_id ?? null,
            })} />
          : <Notice tone="caution" title="Top claims are unavailable">{band.error}</Notice>}
        <Suspense fallback={<AddDropSkeleton />}>
          <AddDropPanel apiPath={withTeamId("/api/waivers/add-drop", sel)} sel={addDrop} sp={sp} provider={pv} />
        </Suspense>
      </div>

      <WaiverShortlist players={[...players, ...(band.ok ? band.data.players : [])].map((player) => ({
        id: player.espn_player_id, name: player.full_name, position: player.position, team: player.pro_team,
      }))} team={perspective?.team_id ?? null} teamName={perspective?.name ?? null} provider={pv}
        leagueName={league?.name ?? null} />

      <Suspense fallback={<Section title="Future-week stashes">Checking roster-specific future weeks…</Section>}>
        <StashPanel horizon={readStashHorizon(sp.stashHorizon)} teamId={selectedTeamId(sel)} sp={sp} />
      </Suspense>

      <div className={styles.filters}>
        <Tabs label="Waiver decision views" items={viewItems} current={currentView} />
        <details className="filter-box">
          <summary>
            <span className="filter-box-title">Filters</span>
            <span className="filter-box-summary">{pos === "ALL" ? "All positions" : pos} · {rec === "ALL" ? "Any recommendation" : REC_LABEL[rec]}</span>
          </summary>
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
          {(pos !== "ALL" || rec !== "ALL") && (
            <p className={styles.reset}>
              <span>{[pos !== "ALL" ? pos : null, rec !== "ALL" ? REC_LABEL[rec] : null].filter(Boolean).join(" · ")}</span>
              <Link className="chip reset" href={chipHref({ pos: null, rec: null })}>Reset filters</Link>
            </p>
          )}
          </div>
        </details>
        <SavedViews tool="waivers" />
      </div>

      <Section
        title={<>{VIEWS[view]} <span className={styles.count}>{total} {total === 1 ? "player" : "players"}</span></>}
        flush={pageRows.length > 0}
        footnote={coverage
          ? <>
            {coverage.evaluated} of {coverage.eligible} eligible players in the stored pool evaluated · {coverage.excluded_by_limit} excluded by the per-position evaluation limit. {coverage.limitation}{" "}
            Historical value and xFP are PPR-based heuristics; forward {proj} projections use the league&apos;s scoring. Advice reflects stored snapshots; verify transaction locks and current availability in {pv}.
            {!ownership && <> {OWNERSHIP_UNAVAILABLE}</>}
          </>
          : "Decision coverage is unavailable. Run the analytics refresh to generate decision diagnostics."}
      >
        <WaiverTable rows={pageRows} sort={sort} sp={spNoOffset} empty={empty} ownership={ownership} proj={proj} />
      </Section>

      {/* An empty list already shows its EmptyState; no "0 results" line beneath it. */}
      {ordered.length > 0 && (
        <nav className={styles.pager} aria-label="Waiver pages">
          {offset > 0 && <Link className="chip" href={withParams(sp, { offset: offset - PAGE_SIZE > 0 ? String(offset - PAGE_SIZE) : null })}>Previous</Link>}
          <span className="sub">{`${offset + 1}–${Math.min(offset + pageRows.length, ordered.length)} of ${ordered.length}`}</span>
          {offset + pageRows.length < ordered.length && <Link className="chip" href={withParams(sp, { offset: String(offset + PAGE_SIZE) })}>Next</Link>}
        </nav>
      )}

      <Glossary terms={["Waiver score", "Value rating", "xFP", "ROS", "FAAB", "—"]} />
      <Method title="How waiver advice is calculated">
        <div className={styles.method}>
          <p>
            Advice combines four separate pieces of evidence. The existing opportunity, fit and waiver scores stay as compatibility diagnostics;
            the recommendation itself does not come from the blended score. The waiver score stays on its own 0–10 scale; the opportunity and value ratings in the evidence are 0–100 relative ranks (legacy 0–10 values are labelled as such).
          </p>
          <ol>
            <li><b>Player quality.</b> Historical opportunity and production, role trend, stored forward {proj} projections and context headlines. Unknown value stays unknown in a comparison.</li>
            <li><b>Roster benefit.</b> The optimal current and fully comparable future starting lineups before and after the actual add and drop. A missing projection is never zero; a supplied zero is real. Future weeks use {proj === "ESPN" ? "unmodified ESPN points" : "Fantasy Intel's own projections (Ours)"} and known byes, with IR excluded.</li>
            <li><b>Feasibility and sacrifice.</b> Active capacity, positional caps, IR eligibility, snapshot freshness (72 hours) and starting-slot coverage. Every active single-player drop is considered; rejected choices and defensible alternatives are listed.</li>
            <li><b>Resources.</b> Free agent, waiver priority, FAAB or unknown rules. Priority in the top three is preserved for marginal gains. No precise bids and no claim-success probabilities; the claim plan below gives heuristic FAAB ranges where the league supplies enough.</li>
          </ol>
          <p>
            Thresholds are decision aids, not calibrated probabilities: a historical starter gain of at least 1 on the internal 0–10 value scale (about 10 points of the 0–100 value rating), a fully comparable forward gain of at least
            1 {proj} point per week, and for a stash an internal value of at least 6 on that 0–10 scale with a same-position depth gain of at least 1 plus a real need or a rising role. Strong but unproven value stays at Monitor.
          </p>
          <p>
            Kickers and defenses are compared forward against the team&apos;s weakest required starter. A gain of at least 1 upcoming point is a short-term stream; gains in two of the next three matchups make a multiweek hold.
            Lines and opponent context are shown as facts and never converted into invented fantasy points.
          </p>
          <p>
            Competition is a coarse bucket from managers&apos; past adds and positional needs. It does not predict bids. Candidate coverage is the locally stored {pv} pool, up to 60 players per position
            chosen from four lanes (forward projection, depth-chart role, historical value{ownership ? ", ownership" : ""}); it is not the whole NFL. Each pickup is a separate scenario against the team&apos;s current roster.
          </p>
          <p>
            The claim plan is the shortlist in your claim order, saved only on this device for this league and team. Each claim is checked with the
            add/drop simulator above against the current roster; claims that share a drop or an open roster spot are flagged on the later claim, and
            bids are checked only against the FAAB budget and spend {pv} supplies. A claim shows Checks pass only when every rule it
            depends on is in the synced data and the league data is current. Nothing is submitted to {pv}.
          </p>
          <p>
            FAAB guidance appears only in a league whose synced settings confirm FAAB bidding and whose budget and your spend are both synced; an
            unknown budget is never taken as $0. Each claim&apos;s range is a heuristic, labelled as one: urgency comes from the simulator&apos;s
            rest-of-season lineup gain per remaining week, and the range is a band of the share of your remaining FAAB you choose (at least an even
            split over the remaining weeks), rounded down and never above what you have left. The plan&apos;s ranges are also checked together:
            claims that drop the same player or need the same open spot cannot all go through, and when the rest would still overspend, each
            claim shows a top that fits. League winning bids are shown as observed, from executed waiver claims only, and never change a range;
            losing and pending bids are private. What {pv} does not supply (a minimum bid, bid history, the next deadline) is listed as unknown.
            No claim-success probability is given.
          </p>
        </div>
        <p>
          <RulesLink section="transactions">Waiver, FAAB and roster rules used here</RulesLink>
        </p>
      </Method>
    </div>
    </ShortlistProvider>
  );
}
