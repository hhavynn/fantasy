import Link from "next/link";
import { PlayerIdentity } from "@/components/PlayerIdentity";
import { Delta, EmptyState, Section, SortableTh, VisuallyHidden } from "@/components/ui";
import { Missing } from "@/lib/format";
import { plural } from "@/lib/plural";
import {
  DEFAULT_SORT, PCT_MISSING, SORT_KEYS, moverSortValue, moversHref, notComparableLines, pctText, rankText, rankTitle, totalsText,
  type MoversResponse, type MoversState,
} from "@/lib/projection-movers";
import { tidyReason } from "@/lib/reason-text";
import { parseSort, sortRows, type SearchParams } from "@/lib/sort";
import styles from "./movers.module.css";

/** Every compared player, sortable by link. The backend caps the list at its largest changes. */
export function MoversTable({ d, state, sp }: { d: MoversResponse; state: MoversState; sp: SearchParams }) {
  const sort = parseSort(sp, SORT_KEYS, DEFAULT_SORT);
  const rows = sortRows(d.players, (m) => moverSortValue(m, sort.key), sort.dir);
  const s = d.summary;
  const shown = rows.length;
  const nc = notComparableLines(s);
  const totals = totalsText(d.window.weeks);
  const th = (key: string, label: string, title?: string, align: "left" | "right" = "right") => (
    <SortableTh sortKey={key} label={label} current={sort} searchParams={sp} align={align} title={title} />
  );
  return (
    <Section
      title="All compared players"
      subtitle={sort.key === "size" ? "Largest change first" : undefined}
      flush={shown > 0}
      footnote={
        <>
          {shown < s.compared && <>The table lists the {shown} largest of {s.compared} compared players. </>}
          {s.not_comparable > 0 && (
            <>
              {s.not_comparable} {plural(s.not_comparable, "player")} could not be compared because a projection is missing in at least one week
              {nc.lines.length > 0 && <>: {nc.lines.map((l) => `${tidyReason(l.reason)} (${l.n})`).join("; ")}{nc.more > 0 ? `; and ${nc.more} more ${plural(nc.more, "reason")}` : ""}</>}
              . A missing projection is never counted as 0.
            </>
          )}
        </>
      }
    >
      {shown === 0 ? (
        <EmptyState title="No players to show">No player has a projection in every compared week in both forecasts.</EmptyState>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                {th("player", "Player", "Sort by name", "left")}
                {th("before", "Earlier", `${totals} in the earlier forecast`)}
                {th("after", "Latest", `${totals} in the latest forecast`)}
                {th("delta", "Change", "Latest minus earlier, in projected points")}
                {th("pct", "%", "Change as a share of the earlier total; blank when that total is too small")}
                {th("rank", "Rank move", "Rank among players at the same position by total; sorts by places moved up")}
                <th scope="col"><span className="faint">Detail</span></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((m) => (
                <tr key={m.espn_player_id}>
                  <td className={styles.tablePlayer}>
                    <PlayerIdentity id={m.espn_player_id} name={m.name} position={m.position} team={m.nfl_team} />
                  </td>
                  <td className="num">{m.before.toFixed(1)}</td>
                  <td className="num">{m.after.toFixed(1)}</td>
                  <td className="num"><Delta value={m.delta} /></td>
                  <td className="num">{pctText(m.pct) ?? <Missing why={PCT_MISSING} />}</td>
                  <td className="num" title={rankTitle(m)}>{rankText(m) ?? <Missing why={rankTitle(m)} />}</td>
                  <td>
                    <Link className={styles.detailLink} href={`${moversHref(state, { player: m.espn_player_id })}#detail`} prefetch={false}>
                      Details<VisuallyHidden> for {m.name}</VisuallyHidden>
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}
