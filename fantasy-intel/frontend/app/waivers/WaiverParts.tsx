import { PlayerLink } from "@/components/PlayerLink";
import { Badge, Pos, TrendArrow } from "@/components/ui";
import { label, num } from "@/lib/format";
import { ACTIONS } from "./helpers";
import type { WaiverRow } from "./types";

/** The move the analysis recommends, as a short label ("Add now", "No legal move identified"). */
export function actionLabel(row: WaiverRow): string | null {
  const d = row.components.decision;
  return d ? ACTIONS[d.action] ?? label(d.action) : null;
}

export function EvidenceBadge({ row }: { row: WaiverRow }) {
  const d = row.components.decision;
  if (!d) return null;
  return d.confidence === "SUPPORTED"
    ? <Badge value="Evidence supported" tone="info" />
    : <Badge value="Limited evidence" tone="warn" title="The analysis lists evidence limits for this move; see the evidence below" />;
}

/** The player to drop, or why there is none. */
export function GiveUp({ row }: { row: WaiverRow }) {
  const d = row.components.decision;
  if (!d) return <span className="missing" title="Refresh analytics to generate roster advice">—</span>;
  const drop = d.roster_move.drop;
  if (drop) return <><PlayerLink id={drop.espn_player_id} name={drop.name} /> <Pos value={drop.position} /></>;
  switch (d.roster_move.status) {
    case "ADD_WITHOUT_DROP":
      return <span title={d.roster_move.summary}>None needed</span>;
    case "MOVE_REQUIRED":
      return <span className="faint" title={d.roster_move.summary}>Drop needed, none found</span>;
    case "INCOMPATIBLE":
      return <span className="faint" title={d.roster_move.summary}>No legal drop</span>;
    default:
      return <span className="faint" title={d.roster_move.summary}>Unknown</span>;
  }
}

export function RoleTrend({ trend }: { trend: string | null }) {
  if (!trend) return <span className="missing" title="No role trend">—</span>;
  return <span><TrendArrow label={trend} /> {trend === "INSUFFICIENT" ? "Too early" : label(trend)}</span>;
}

/** League competition as a coarse behavioural bucket (never a forecast of bids). */
export function Competition({ row }: { row: WaiverRow }) {
  const names = row.competitors.map((c) => c.name);
  return (
    <Badge
      value={row.league_interest}
      title={names.length
        ? `Likely competitors: ${names.join(", ")}. Historical tendencies and needs; no prediction of bids.`
        : "Historical tendencies and needs; no prediction of bids."}
    />
  );
}

export function OwnedDelta({ row }: { row: WaiverRow }) {
  const c = row.percent_change;
  return (
    <>
      {num(row.percent_owned, 1)}%{" "}
      {c === null || c === undefined
        ? null
        : <span className={c > 0 ? "up" : c < 0 ? "down" : "faint"} title="Change in ESPN % owned this week">{c > 0 ? "+" : c < 0 ? "−" : ""}{Math.abs(c).toFixed(1)}</span>}
    </>
  );
}
