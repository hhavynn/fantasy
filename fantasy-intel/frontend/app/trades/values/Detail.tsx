import Link from "@/components/LeagueLink";
import { CompareLink } from "@/components/CompareLink";
import { PlayerIdentity } from "@/components/PlayerIdentity";
import { Delta, Section, Stat, StatStrip } from "@/components/ui";
import { Missing, when } from "@/lib/format";
import { plural } from "@/lib/plural";
import type { SignalProvenance } from "@/lib/signal-provenance";
import {
  VALUE_MISSING, changeUnavailableText, changeWindowNote, ownerHref, positionRankText, signedPts, sourceText, weeksText,
  whyText, type TradeValuesResponse, type ValueRow,
} from "@/lib/trade-values";
import { AnalyzerLink } from "./AnalyzerLink";
import styles from "./values.module.css";

const STATUS: Record<string, string> = {
  counted: "Counted", nfl_ineligible: "Not NFL-eligible", missing: "No projection",
  no_replacement: "No replacement level", not_counted: "Not counted",
};
const NO_LEVEL = "No replacement level: no projected free agent and no rostered player beyond the starters; never counted as 0";
const REPLACEMENT: Record<string, string> = { free_agent: "best free agent", league_structure: "league structure" };

function changeNote(d: TradeValuesResponse, r: ValueRow, before: number | null): string | undefined {
  if (!d.change?.available || before === null) return undefined;
  const span = changeWindowNote(d.change);
  const now = r.change?.value_now ?? null;
  // Over fewer weeks than the value, the change compares the two values over those weeks: show both, with the weeks.
  return span && now !== null ? `${before.toFixed(1)} to ${now.toFixed(1)} ${span}` : `from ${before.toFixed(1)}`;
}

/** One player's value, shown as its calculation: version, week-by-week sum, why it moved, caveats and next steps. */
export function Detail({ d, r, projections }: { d: TradeValuesResponse; r: ValueRow; projections: SignalProvenance | null }) {
  const b = d.basis!;
  const why = whyText(r, d.change);
  const label = r.name ?? `Player #${r.espn_player_id}`;
  const before = r.change?.value_before ?? null;
  const mixed = r.projection_versions.length > 1;
  return (
    <Section id="calculation" title={`How ${label}'s value is calculated`}
      subtitle={`${b.label}, calculation ${b.calculation_version}. Not a trade price.`}>
      <div className={styles.detailHead}>
        <PlayerIdentity id={r.espn_player_id} name={r.name} position={r.position} team={r.pro_team} size="lg" />
        <p className={styles.detailLinks}>
          <Link href={`/players?id=${r.espn_player_id}`}>Profile</Link>
          <CompareLink a={r.espn_player_id} aName={r.name} />
          <Link href={ownerHref(r.team_id)}>Owner: {r.team_name ?? `Team ${r.team_id}`}{r.on_my_team ? " (you)" : ""}</Link>
          <AnalyzerLink row={r} myTeamId={d.my_team_id} suffix=" in the analyzer" />
        </p>
      </div>
      <StatStrip compact>
        <Stat label="Value" value={r.value !== null ? r.value.toFixed(2) : <Missing why={r.no_replacement_weeks.length ? NO_LEVEL : VALUE_MISSING} />}
          note={r.value === null && r.known_value !== null ? `Known weeks only: ${r.known_value.toFixed(1)}` : "points above replacement"} />
        <Stat label="Position rank" value={positionRankText(r) ?? <Missing why="No rank without a value" />}
          note={r.league_starters_at_position !== null ? `${r.league_starters_at_position} start at ${r.position} in this league` : undefined} />
        <Stat label="Overall rank" value={r.rank !== null ? `${r.rank} of ${r.rank_of}` : <Missing why={r.rank_reason ?? "Not ranked"} />}
          note={r.rank !== null ? "players valued above a free agent" : r.rank_reason ?? undefined} />
        <Stat label="Change" value={d.change?.available && r.change?.delta != null ? <Delta value={r.change.delta} /> : "Unavailable"}
          note={changeNote(d, r, before)} />
      </StatStrip>

      <div className={styles.detailGrid}>
        <div className={styles.detailText}>
          <h3 className={styles.panelTitle}>Why it moved</h3>
          <p className={styles.muted}>{why ?? changeUnavailableText(d.change)}</p>
          <h3 className={styles.panelTitle}>Sample window</h3>
          <p className={styles.muted}>
            {r.weeks_counted} of {b.window.weeks.length} remaining {plural(b.window.weeks.length, "week")} counted ({weeksText(b.window.weeks)}).
            {r.value_regular !== null && r.value_playoff !== null && b.window.playoff_weeks.length > 0 &&
              <> Regular season {r.value_regular.toFixed(1)}, fantasy playoffs {r.value_playoff.toFixed(1)}.</>}
          </p>
          <h3 className={styles.panelTitle}>Uncertainty</h3>
          {r.uncertainty.length ? (
            <ul className={styles.caveatList}>{r.uncertainty.map((u) => <li key={u.code}>{u.text}</li>)}</ul>
          ) : (
            <p className={styles.muted}>No missing projection, eligibility or replacement caveat. The value is still a projection, and projections can be wrong.</p>
          )}
          <h3 className={styles.panelTitle}>Source and version</h3>
          <p className={styles.muted}>
            {sourceText({ ...b, projection_versions: r.projection_versions })}
            {projections?.captured_at ? <>, as of {when(projections.captured_at)}</> : null}
            {projections?.snapshot_version ? <>, data version <code>{projections.snapshot_version}</code></> : null}. {b.scoring}
          </p>
        </div>
        <div className="table-wrap">
          <table className={styles.weekTable}>
            <caption className={styles.caption}>Value = sum of “Adds”, week by week (each rounded so they add up exactly)</caption>
            <thead>
              <tr>
                <th scope="col">Week</th>
                <th scope="col" className="num">Projection</th>
                <th scope="col" className="num">Replacement</th>
                <th scope="col" className="num">Adds</th>
                <th scope="col">Status</th>
                {mixed && <th scope="col">Source</th>}
              </tr>
            </thead>
            <tbody>
              {r.weekly.map((w) => (
                <tr key={w.week}>
                  <th scope="row">{w.week}{w.phase === "playoff" ? <span className="faint"> playoffs</span> : null}</th>
                  <td className="num">{w.projection !== null ? w.projection.toFixed(2) : <Missing why="No projection this week; never counted as 0" />}</td>
                  <td className="num" title={w.replacement_source ? REPLACEMENT[w.replacement_source] : undefined}>
                    {w.replacement !== null ? w.replacement.toFixed(2) : <Missing why={NO_LEVEL} />}
                    {w.replacement_source === "league_structure" && <span className="faint"> LS</span>}
                  </td>
                  <td className="num">{w.adds !== null ? w.adds.toFixed(2) : <Missing why={STATUS[w.status]} />}</td>
                  <td>{STATUS[w.status] ?? w.status}</td>
                  {mixed && <td>{w.version ?? "—"}</td>}
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr>
                <th scope="row" colSpan={3}>Value</th>
                <td className="num">{r.value !== null ? r.value.toFixed(2) : <Missing why={r.no_replacement_weeks.length ? NO_LEVEL : VALUE_MISSING} />}</td>
                <td colSpan={mixed ? 2 : 1} className="faint">{r.value !== null ? "sum of Adds" : "unknown"}</td>
              </tr>
            </tfoot>
          </table>
          {r.weekly.some((w) => w.replacement_source === "league_structure") && (
            <p className={styles.caption}>LS: league-structure replacement level (no free agent at the position was projected that week).</p>
          )}
          {r.change && r.change.projection_change !== null && (
            <p className={styles.caption}>Own projection change over the compared weeks: {signedPts(r.change.projection_change)}.</p>
          )}
        </div>
      </div>
    </Section>
  );
}
