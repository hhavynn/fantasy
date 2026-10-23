import { CompareLink } from "@/components/CompareLink";
import { PlayerIdentity } from "@/components/PlayerIdentity";
import { TeamLink } from "@/components/TeamLink";
import { Badge, Delta, EmptyState, Rating, Section, TrendArrow } from "@/components/ui";
import { addDropHref } from "@/lib/add-drop";
import { cx } from "@/lib/cx";
import { int, num } from "@/lib/format";
import { ownershipAvailable, type Provider } from "@/lib/labels";
import { withLeague } from "@/lib/league-scope";
import { currentLeague } from "@/lib/league-scope.server";
import { RATING_CAVEAT, RATING_DESCRIPTION, ratingFor } from "@/lib/ratings";
import { nextSteps } from "@/lib/roster-status";
import { hasInjuryDesignation, ownerBadge } from "./helpers";
import styles from "./players.module.css";
import type { PlayerListRow } from "./types";

/** GET form that keeps the query in the URL (`/players?q=`); on a league page it stays there (`/l/<lg>/players?q=`). */
export async function SearchForm({ q, compact = false, autoFocus = false }: { q: string; compact?: boolean; autoFocus?: boolean }) {
  const action = withLeague("/players", (await currentLeague())?.id);
  return (
    <form className={cx(styles.search, compact ? styles.searchCompact : styles.searchBar)} action={action} role="search">
      <input
        className={styles.input}
        type="search"
        name="q"
        defaultValue={q}
        placeholder={compact ? "Search players" : "Search by name (e.g. Lloyd)"}
        aria-label="Search players by name"
        autoFocus={autoFocus}
      />
      <button className={styles.submit} type="submit">Search</button>
    </form>
  );
}

const OPP_TITLE = `Opportunity rating, 0–100: ${RATING_DESCRIPTION.opportunity}. ${RATING_CAVEAT}`;

export function ResultsTable({ rows, q, limit, provider = "espn" }: { rows: PlayerListRow[]; q: string; limit: number; provider?: Provider }) {
  // ESPN-wide ownership exists only for ESPN leagues; a Sleeper league lists players without it.
  const owned = ownershipAvailable(provider);
  if (rows.length === 0) {
    return (
      <Section title={`Results for “${q}”`}>
        <EmptyState title={`No players match “${q}”`}>
          <p>Check the spelling or try just a last name. Accents are ignored, and kickers and team defenses are searchable too.</p>
        </EmptyState>
      </Section>
    );
  }
  return (
    <Section
      title={q ? `Results for “${q}”` : owned ? "Most-owned players" : "Players"}
      actions={<span className="faint">{rows.length} {rows.length === 1 ? "player" : "players"}</span>}
      footnote={owned ? `${q
        ? (rows.length >= limit ? `Showing the first ${limit} matches, most-owned first. Add more of the name to narrow them.` : "Most-owned first.")
        : `The ${limit} most-owned players across ESPN. Search above to find anyone else.`} Opportunity is a 0–100 relative rank within position, not a projection.`
        : `${q
        ? (rows.length >= limit ? `Showing the first ${limit} matches. Add more of the name to narrow them.` : "")
        : `The first ${limit} players. Search above to find anyone else.`} Ownership % is not available for Sleeper leagues. Opportunity is a 0–100 relative rank within position, not a projection.`.trim()}
      flush
    >
      <div className="table-wrap">
        <table className={styles.results}>
          <thead>
            <tr>
              <th>Player</th><th>Fantasy team</th>
              {owned && <th className="num" title="Percent of ESPN leagues that roster the player">% own</th>}
              <th className="num" title="NFL games with a usage record this season">G</th>
              <th className="num" title="PPR points per game (nflverse)">PPR/g</th>
              <th className="num" title="Expected PPR points per game from role (nflverse xFP): what an average player would score with this workload">xFP/g</th>
              <th className="num" title="PPR per game minus expected PPR per game. Above zero: producing more than the role implies">+/− xFP</th>
              <th title="Usage trend">Trend</th><th title="Market signal">Signal</th>
              <th className="num" title={OPP_TITLE}>Opportunity</th>
              <th title="Where to take this player next: compare him, simulate adding him on Waivers, or look for trades for him">Next</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => {
              const owner = ownerBadge(p.fantasy_team, p.availability_status, provider);
              const steps = nextSteps(p.availability_status, p.fantasy_team, provider);
              return (
                <tr key={p.espn_player_id}>
                  <td>
                    <PlayerIdentity id={p.espn_player_id} name={p.full_name} position={p.position} team={p.pro_team}
                                    meta={hasInjuryDesignation(p.injury_status) ? <Badge value={p.injury_status} /> : null} />
                  </td>
                  <td><Badge value={owner.text} tone={owner.tone} title={owner.title} /></td>
                  {owned && <td className="num dim">{num(p.percent_owned, 1)}</td>}
                  <td className="num dim">{int(p.games, "No games / no NFL id")}</td>
                  <td className="num key">{num(p.fp_per_game, 1, "No games / no NFL id")}</td>
                  <td className="num">{num(p.xfp_per_game, 1, "No games / no NFL id")}</td>
                  <td className="num"><Delta value={p.fp_minus_xfp} /></td>
                  <td><TrendArrow label={p.trend_label} /></td>
                  <td><Badge value={p.market_signal} /></td>
                  <td className="num"><Rating rating={ratingFor(p, "opportunity")} kind="opportunity" variant="bar" why="No usage data" /></td>
                  <td>
                    <span className={styles.next}>
                      <CompareLink a={p.espn_player_id} aName={p.full_name} />
                      {/* Adds only for a player off every roster; trades only for a rostered one. Unknown gets neither. */}
                      {steps.add && (
                        <TeamLink href={addDropHref("/waivers", {}, { add: p.espn_player_id, drop: null })} className={styles.nextLink}
                          aria-label={`Simulate adding ${p.full_name} on Waivers`}>Simulate add</TeamLink>
                      )}
                      {steps.trade && (
                        <TeamLink href={`/trades/discover?mode=targeted&target=${p.espn_player_id}`} className={styles.nextLink}
                          aria-label={`Find trades for ${p.full_name}`}>Find trades</TeamLink>
                      )}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Section>
  );
}
