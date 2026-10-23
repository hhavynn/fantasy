import { CompareLink } from "@/components/CompareLink";
import { PlayerIdentity } from "@/components/PlayerIdentity";
import { TeamLink } from "@/components/TeamLink";
import { EmptyState, Pos, Section, SortableTh, VisuallyHidden } from "@/components/ui";
import { Missing } from "@/lib/format";
import { plural } from "@/lib/plural";
import {
  cellTitle, columns, countsSentence, formatValue, ordinal, sampleNote, waiverHref, weeksText,
  type MetricInfo, type ScreenResponse, type ScreenRow, type ScreenerState, type SearchParams,
} from "@/lib/screener";
import styles from "./screener.module.css";

function PlayerCell({ r }: { r: ScreenRow }) {
  const id = r.ownership.player_id;
  if (id !== null) return <PlayerIdentity id={id} name={r.name} position={r.position} team={r.team} />;
  return (
    <span className={styles.unlinked} title={`${r.name}: not linked to a player in this league, so there is no profile page`}>
      <span className={styles.unlinkedName}>{r.name}</span>
      <span className={styles.unlinkedMeta}><Pos value={r.position} /> <span>{r.team ?? "—"}</span></span>
    </span>
  );
}

function MetricTd({ r, m }: { r: ScreenRow; m: MetricInfo }) {
  const c = r.metrics[m.key];
  if (!c) return <td className={`num ${styles.na}`} title={cellTitle(c, m)}>n/a</td>;
  const value = formatValue(c.value, m.unit);
  const note = sampleNote(c);
  return (
    <td className="num" title={cellTitle(c, m)}>
      {value === null ? <Missing why={cellTitle(c, m)} /> : value}
      {note && <span className={styles.sample}>{note}</span>}
      {c.percentile !== undefined && value !== null && <span className={styles.pctl}>{ordinal(c.percentile)} pct</span>}
    </td>
  );
}

function OwnershipCell({ r }: { r: ScreenRow }) {
  const o = r.ownership;
  if (o.status === "unknown") {
    return <span className={styles.unknown} title={o.reason ?? "Ownership is unknown"}>{o.label}</span>;
  }
  if (o.status === "rostered") {
    const team = o.team_name ?? (o.team_id !== null ? `Team ${o.team_id}` : "a team");
    return <span title={`Rostered by ${team}`}>{o.is_mine ? "Your roster" : team}</span>;
  }
  return <span className={styles.available}>{o.label}</span>;
}

/** The matched players, sorted by the backend (sort links reload the page with ?sort=&dir=). */
export function Results({ d, state, sp }: { d: ScreenResponse; state: ScreenerState; sp: SearchParams }) {
  const cols = columns(state, d.query.sort, d.metrics);
  const current = { key: d.query.sort, dir: d.query.dir };
  const counts = d.counts!;
  const short = (key: string) => d.metrics.find((m) => m.key === key)?.short ?? key;
  const th = (key: string, label: string, title: string, align: "left" | "right" = "right") => (
    <SortableTh sortKey={key} label={label} current={current} searchParams={sp} align={align} title={title} />
  );
  return (
    <Section
      title="Matches"
      subtitle={countsSentence(counts)}
      flush={d.rows.length > 0}
      footnote={
        <>
          Each value is over the player&apos;s games in the window ({d.data?.window ? `weeks ${d.data.window.from}–${d.data.window.to}` : "the weeks shown"}
          {d.query.last ? `, his last ${d.query.last} ${plural(d.query.last, "game")} there` : ""}); a smaller count under a value means some games
          lacked its inputs. — is missing, never 0. Percentiles rank a player among his position in this window
          (at least {d.limits.percentile_min_pool} players with the metric).
          {d.truncated && <> Showing the first {counts.shown} of {counts.matched - counts.ownership_excluded}; raise Rows to see more.</>}
        </>
      }
    >
      {d.rows.length === 0 ? (
        <EmptyState title="No player matches this screen">
          Loosen a filter, widen the weeks or lower the minimum games. Players the screen could not decide are listed below.
        </EmptyState>
      ) : (
        <div className="table-wrap">
          <table className={styles.table}>
            <caption className={styles.caption}>
              <VisuallyHidden>Players matching the screen, {plural(d.rows.length, "row")}. </VisuallyHidden>
            </caption>
            <thead>
              <tr>
                {th("name", "Player", "Sort by name", "left")}
                {th("games", "G", "Games played in the window")}
                {cols.map((m) => <SortableTh key={m.key} sortKey={m.key} label={m.short} current={current} searchParams={sp}
                  align="right" title={`${m.label}, ${m.unit_label}. ${m.definition}`} />)}
                <th scope="col">League</th>
                <th scope="col" title="Shown metrics a row lacks for some or all of its games">Data</th>
                <th scope="col"><span className="faint">Links</span></th>
              </tr>
            </thead>
            <tbody>
              {d.rows.map((r) => {
                const waiver = waiverHref(r.ownership);
                const gaps = r.incomplete.filter((g) => cols.some((c) => c.key === g.metric));
                return (
                  <tr key={r.gsis_id} className={r.ownership.is_mine ? "mine" : undefined}>
                    <td className={styles.playerCell}><PlayerCell r={r} /></td>
                    <td className="num" title={weeksText(r.weeks)}>{r.games}</td>
                    {cols.map((m) => <MetricTd key={m.key} r={r} m={m} />)}
                    <td className={styles.ownerCell}><OwnershipCell r={r} /></td>
                    <td className={styles.gaps}>
                      {gaps.length === 0 ? <span className="faint">Complete</span> : (
                        <span title={gaps.map((g) => `${short(g.metric)}: ${g.state === "missing" ? "missing" : `${g.n} of ${g.of} games`}`).join("; ")}>
                          {gaps.length} {plural(gaps.length, "gap")}
                        </span>
                      )}
                    </td>
                    <td className={styles.links}>
                      {r.ownership.player_id !== null && <CompareLink a={r.ownership.player_id} aName={r.name} />}
                      {waiver && (
                        <TeamLink href={waiver} prefetch={false}>
                          Add/drop<VisuallyHidden> {r.name}</VisuallyHidden>
                        </TeamLink>
                      )}
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
