import { PlayerLink } from "@/components/PlayerLink";
import { Badge, EmptyState, Pos, Section, TrendArrow } from "@/components/ui";
import { cx } from "@/lib/cx";
import { int, num } from "@/lib/format";
import { hasInjuryDesignation, ownerBadge } from "./helpers";
import styles from "./players.module.css";
import type { PlayerListRow } from "./types";

/** GET form that keeps the query in the URL (`/players?q=`). */
export function SearchForm({ q, compact = false, autoFocus = false }: { q: string; compact?: boolean; autoFocus?: boolean }) {
  return (
    <form className={cx(styles.search, compact ? styles.searchCompact : styles.searchBar)} action="/players" role="search">
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

const OPP_TITLE = "Opportunity score, 0–10: recent expected fantasy points (xFP) percentile at the position";

export function ResultsTable({ rows, q, limit }: { rows: PlayerListRow[]; q: string; limit: number }) {
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
      title={q ? `Results for “${q}”` : "Most-owned players"}
      actions={<span className="faint">{rows.length} {rows.length === 1 ? "player" : "players"}</span>}
      footnote={q
        ? (rows.length >= limit ? `Showing the first ${limit} matches, most-owned first. Add more of the name to narrow them.` : "Most-owned first.")
        : `The ${limit} most-owned players across ESPN. Search above to find anyone else.`}
      flush
    >
      <div className="table-wrap">
        <table className={styles.stickyFirst}>
          <thead>
            <tr>
              <th>Player</th><th>NFL</th><th>Fantasy team</th><th>Status</th>
              <th className="num" title="Percent of ESPN leagues that roster the player">% own</th>
              <th className="num" title="NFL games with a usage record this season">G</th>
              <th className="num" title="PPR points per game (nflverse)">PPR/g</th>
              <th className="num" title="Expected PPR points per game from role (nflverse xFP)">xFP/g</th>
              <th title="Usage trend">Trend</th><th title="Market signal">Signal</th>
              <th className="num" title={OPP_TITLE}>Opp</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => {
              const owner = ownerBadge(p.fantasy_team, p.availability_status);
              return (
                <tr key={p.espn_player_id}>
                  <td><PlayerLink id={p.espn_player_id} name={p.full_name} /> <Pos value={p.position} /></td>
                  <td>{p.pro_team ?? "FA"}</td>
                  <td><Badge value={owner.text} tone={owner.tone} /></td>
                  <td>{hasInjuryDesignation(p.injury_status) ? <Badge value={p.injury_status} /> : null}</td>
                  <td className="num">{num(p.percent_owned, 1)}</td>
                  <td className="num">{int(p.games, "No games / no NFL id")}</td>
                  <td className="num">{num(p.fp_per_game, 1, "No games / no NFL id")}</td>
                  <td className="num">{num(p.xfp_per_game, 1, "No games / no NFL id")}</td>
                  <td><TrendArrow label={p.trend_label} /></td>
                  <td><Badge value={p.market_signal} /></td>
                  <td className="num">{num(p.opportunity_score, 1, "No usage data")}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Section>
  );
}
