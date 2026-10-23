import { PlayerIdentity } from "@/components/PlayerIdentity";
import { Delta, SortableTh } from "@/components/ui";
import { Missing } from "@/lib/format";
import {
  deltaMissingText, deltaSentence, rangeHorizonNote, rangeMetaText, rangeText, sideMissingText, sideRankText,
  type CompareResponse, type CompareRow, type CompareScope, type CompareSide,
} from "@/lib/projection-compare";
import { formatPoints } from "@/lib/rankings";
import type { SearchParams, SortState } from "@/lib/sort";
import { RosterBadge } from "../RankingParts";
import styles from "./compare.module.css";

/** A rank, or the reason in words there is none (a muted dash with the reason as its title). */
function RankCell({ side, scope }: { side: CompareSide; scope: CompareScope }) {
  const text = sideRankText(side, scope);
  if (text) return <strong className={styles.rank}>{text}</strong>;
  return <span className={styles.noRank} title={side.reason ?? undefined}>{sideMissingText(side)}</span>;
}

function PointsCell({ side }: { side: CompareSide }) {
  return side.value === null || side.value === undefined
    ? <Missing why={side.reason ?? sideMissingText(side)} />
    : <>{formatPoints(side.value)}</>;
}

function GapCell({ row, providerName, scope }: { row: CompareRow; providerName: string; scope: CompareScope }) {
  const d = row.delta;
  const missing = deltaMissingText(d, providerName);
  if (missing) return <span className={styles.noRank} title="One side has no rank, so there is no gap (never 0)">{missing}</span>;
  return (
    <span className={styles.gap} title={deltaSentence(d, providerName, scope) ?? undefined}>
      <Delta value={d.rank} digits={0} />
      {d.large && <span className={styles.largeTag}>Large</span>}
    </span>
  );
}

/**
 * The comparison table: each source's rank and points side by side, the rank gap (provider minus Ours; positive means
 * Ours ranks him higher) and points gap, and the next-week range. A source with no values in this league is headed
 * "Provider" and never named; a missing side is a word, never 0.
 */
export function CompareTable({ data, sort, sp }: { data: CompareResponse; sort: SortState; sp: SearchParams }) {
  const [provider, ours] = data.sources;
  const providerName = data.provider.name;
  const providerHead = provider.available ? providerName : "Provider";
  const scope = data.scope;
  const scopeWord = scope === "position" ? "position rank" : "overall rank (all positions)";
  const rangeNote = rangeHorizonNote(data.horizon, data.ranges.week);
  return (
    <div className="table-wrap">
      <table className={styles.table} aria-label="Projection sources compared">
        <thead>
          <tr>
            <SortableTh sortKey="name" label="Player" current={sort} searchParams={sp} defaultDir="asc" className={styles.playerCol} />
            <SortableTh sortKey="provider_rank" label={`${providerHead} rank`} current={sort} searchParams={sp} defaultDir="asc"
              title={provider.available ? `${providerName}'s ${scopeWord} from its own published projection` : (provider.reason ?? "Unavailable")} />
            <SortableTh sortKey="ours_rank" label="Ours rank" current={sort} searchParams={sp} defaultDir="asc"
              title={ours.available ? `Fantasy Intel's ${scopeWord} from its own model` : (ours.reason ?? "Unavailable")} />
            <SortableTh sortKey="delta" label="Rank gap" align="right" current={sort} searchParams={sp}
              title={`${providerHead} rank minus Ours rank: positive means Ours ranks him higher`} />
            <th scope="col" className={styles.right} title={`${providerHead} projected points, ${data.window.label}`}>{providerHead} pts</th>
            <th scope="col" className={styles.right} title={`Ours projected points, ${data.window.label}`}>Ours pts</th>
            <SortableTh sortKey="points" label="Pts gap" align="right" current={sort} searchParams={sp}
              title={`Ours minus ${providerHead} projected points, ${data.window.label}`} />
            <th scope="col" title={`Floor to ceiling (P10–P90) if he plays, ${rangeNote}`}>Range ({rangeNote.split(";")[0]})</th>
            <th scope="col">Roster</th>
          </tr>
        </thead>
        <tbody>
          {data.players.map((r) => (
            <tr key={r.espn_player_id} className={r.roster.is_mine ? "mine" : undefined}>
              <td className={styles.playerCol}>
                <PlayerIdentity id={r.espn_player_id} name={r.full_name} position={r.position} team={r.pro_team} />
              </td>
              <td><RankCell side={r.sources.provider} scope={scope} /></td>
              <td><RankCell side={r.sources.ours} scope={scope} /></td>
              <td className="num"><GapCell row={r} providerName={providerName} scope={scope} /></td>
              <td className="num dim"><PointsCell side={r.sources.provider} /></td>
              <td className="num dim"><PointsCell side={r.sources.ours} /></td>
              <td className="num">{r.delta.points === null ? <Missing why="One side has no projection" /> : <Delta value={r.delta.points} />}</td>
              <td className={styles.rangeCell}>
                {r.range && r.range.available ? (
                  <span title={`${r.range.basis}. ${r.range.horizon}. Coverage: ${r.range.coverage.text}.`}>
                    {rangeText(r.range)}
                    <span className={styles.rangeMeta}>{rangeMetaText(r.range)}</span>
                  </span>
                ) : <Missing why={(r.range && !r.range.available && r.range.reason) || "No range"} />}
              </td>
              <td><RosterBadge roster={r.roster} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
