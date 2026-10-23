import Link from "@/components/LeagueLink";
import { CompareLink } from "@/components/CompareLink";
import { PlayerIdentity } from "@/components/PlayerIdentity";
import { Delta, EmptyState, Section, SortableTh, VisuallyHidden } from "@/components/ui";
import { Missing } from "@/lib/format";
import { plural } from "@/lib/plural";
import { parseSort, sortRows, type SearchParams } from "@/lib/sort";
import {
  DEFAULT_SORT, SORT_KEYS, VALUE_MISSING, basisLine, changeUnavailableText, changeWindowNote, ownerHref,
  positionRankText, valueSortValue, valuesHref, type TradeValuesResponse, type ValueRow, type ValuesState,
} from "@/lib/trade-values";
import { AnalyzerLink } from "./AnalyzerLink";
import styles from "./values.module.css";

/** Why a value is missing, from the row's own facts. */
function missingWhy(r: ValueRow): string {
  const why = r.no_replacement_weeks.length && !r.missing_weeks.length
    ? "No value: no measurable replacement level at the position (no projected free agent and no rostered player beyond the starters); never counted as 0"
    : VALUE_MISSING;
  return r.known_value !== null ? `${why}; the known weeks alone sum to ${r.known_value.toFixed(1)}` : why;
}

function ChangeCell({ r, d }: { r: ValueRow; d: TradeValuesResponse }) {
  if (!d.change?.available || !r.change) {
    return <span className={styles.unavailable} title={changeUnavailableText(d.change)}>Unavailable</span>;
  }
  if (r.change.delta === null) {
    return <span className={styles.unavailable} title={r.change.reason ?? "No change can be computed"}>Unavailable</span>;
  }
  const note = changeWindowNote(d.change);
  return <><Delta value={r.change.delta} />{note && <span className={styles.changeWeeks}> {note}</span>}</>;
}

/** Every rostered player in the filter, sortable by link (server-side, URL-backed). Values are the backend's. */
export function ValuesTable({ d, rows, state, sp }: { d: TradeValuesResponse; rows: ValueRow[]; state: ValuesState; sp: SearchParams }) {
  const sort = parseSort(sp, SORT_KEYS, DEFAULT_SORT);
  const sorted = sortRows(rows, (r) => valueSortValue(r, sort.key), sort.dir);
  const unvalued = rows.filter((r) => r.value === null).length;
  const th = (key: string, label: string, title?: string, align: "left" | "right" = "right") => (
    <SortableTh sortKey={key} label={label} current={sort} searchParams={sp} align={align} title={title} />
  );
  const b = d.basis!;
  return (
    <Section
      title="Player values"
      subtitle={basisLine(b)}
      flush={sorted.length > 0}
      footnote={
        <>
          Values count only {b.unit}; they are comparable only with each other on this board. {b.cross_position}
          {unvalued > 0 && <> {unvalued} {plural(unvalued, "player")} {unvalued === 1 ? "has" : "have"} no value because a remaining week has no projection or no measurable replacement level; neither is ever counted as 0.</>}
        </>
      }
    >
      {sorted.length === 0 ? (
        <EmptyState title="No players match these filters">Choose another position or owner.</EmptyState>
      ) : (
        <div className="table-wrap">
          <table className={styles.table}>
            <thead>
              <tr>
                {th("player", "Player", "Sort by name", "left")}
                {th("owner", "Owner", "Sort by owner", "left")}
                {th("value", "Value", `Projected points above replacement, ${b.window.weeks.length} remaining ${plural(b.window.weeks.length, "week")}`)}
                {th("per_week", "Per week", "Value divided by the remaining weeks")}
                {th("pos_rank", "Pos rank", "Rank among rostered players at the position with a known value")}
                {th("change", "Change", "Value now minus the value with last week's stored projections; Unavailable when there is no such snapshot")}
                <th scope="col">Caveats</th>
                <th scope="col"><span className="faint">Links</span></th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((r) => {
                const label = r.name ?? `Player #${r.espn_player_id}`;
                return (
                  <tr key={r.espn_player_id} className={state.player === r.espn_player_id ? styles.selected : undefined}>
                    <td className={styles.playerCell}>
                      <PlayerIdentity id={r.espn_player_id} name={r.name} position={r.position} team={r.pro_team} />
                    </td>
                    <td className={styles.ownerCell}>
                      <Link href={ownerHref(r.team_id)} title={r.team_name ?? undefined}>{r.team_name ?? `Team ${r.team_id}`}</Link>
                      {r.on_my_team && <span className="faint"> (you)</span>}
                    </td>
                    <td className="num">
                      {r.value !== null ? r.value.toFixed(1) : <Missing why={missingWhy(r)} />}
                    </td>
                    <td className="num">{r.value_per_week !== null ? r.value_per_week.toFixed(1) : <Missing why={missingWhy(r)} />}</td>
                    <td className="num">{positionRankText(r) ?? <Missing why="No rank without a value" />}</td>
                    <td className="num"><ChangeCell r={r} d={d} /></td>
                    <td className={styles.caveats} title={r.uncertainty.map((u) => u.text).join(" ") || undefined}>
                      {r.uncertainty.length ? `${r.uncertainty.length} ${plural(r.uncertainty.length, "caveat")}` : <span className="faint">None</span>}
                    </td>
                    <td className={styles.links}>
                      <Link href={`${valuesHref(state, { player: r.espn_player_id }, sp)}#calculation`} prefetch={false}>
                        Details<VisuallyHidden> for {label}</VisuallyHidden>
                      </Link>
                      <CompareLink a={r.espn_player_id} aName={r.name} />
                      <AnalyzerLink row={r} myTeamId={d.my_team_id} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}
