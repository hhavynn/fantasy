import { InteractiveScatter, type InteractivePoint } from "@/components/charts";
import { EmptyState } from "@/components/ui";
import { bubbleRadius } from "@/components/charts/interactive";
import { disruption, median, takeaways, type DstPlayer } from "@/lib/defenses";
import styles from "./defenses.module.css";

const FREE_AGENT = "var(--chart-1)";
const ROSTERED = "var(--chart-6)";

/** Pads a data range by `pad` on each side and rounds it out to whole numbers. */
function domain(values: number[], pad: number): [number, number] {
  return [Math.floor(Math.min(...values) - pad), Math.ceil(Math.max(...values) + pad)];
}

/**
 * Matchup map: the opponent's implied total (left is a weaker offense) against expected sacks + takeaways (up is more
 * splash plays). Bubble area is our expected points, so the big bubbles in the top-left are the streams.
 */
export function DstChart({ players, week, sel }: { players: DstPlayer[]; week: number; sel: number | null }) {
  const usable = players.flatMap((p) => {
    const x = p.independent.context?.opponent_implied_total;
    const y = disruption(p.independent.stat_line);
    const pts = p.independent.expected_points;
    return x === null || x === undefined || y === null || pts === null ? [] : [{ p, x, y, pts }];
  });
  if (usable.length < 3) {
    return (
      <EmptyState title="No matchup map yet">
        Only {usable.length} of {players.length} projected defenses have a betting line so far. The map fills in once lines
        are posted; the table below still ranks every defense.
      </EmptyState>
    );
  }
  // Sized from this week's lowest projection, not zero: D/ST projections sit in a narrow band, so a zero base
  // would draw every bubble nearly the same size.
  const minPts = Math.min(...usable.map((u) => u.pts));
  const span = Math.max(...usable.map((u) => u.pts)) - minPts + 0.5;
  const top = new Set(usable.slice(0, 5).map((u) => u.p.espn_player_id));
  for (const u of usable.filter((u) => !u.p.fantasy_team_id).slice(0, 3)) top.add(u.p.espn_player_id);
  const midX = median(usable.map((u) => u.x))!;
  const midY = median(usable.map((u) => u.y))!;
  const points: InteractivePoint[] = usable.map(({ p, x, y, pts }) => {
    const s = p.independent.stat_line;
    const team = p.nfl_team ?? String(p.espn_player_id);
    return {
      id: String(p.espn_player_id),
      x, y,
      r: bubbleRadius(pts - minPts + 0.5, span, 3, 12),
      name: p.name ?? `${team} D/ST`,
      short: team,
      meta: `${p.is_home ? "vs" : "@"} ${p.opponent ?? "?"} · ${p.fantasy_team_id ? "rostered" : "free agent"}`,
      color: p.fantasy_team_id ? ROSTERED : FREE_AGENT,
      emphasis: p.espn_player_id === sel ? "selected" : null,
      keyLabel: top.has(p.espn_player_id),
      rows: [
        ["Ours", pts.toFixed(1)],
        ["Opp. implied total", x.toFixed(1)],
        ["Sacks", s?.sacks?.toFixed(1) ?? "—"],
        ["Takeaways", takeaways(s)?.toFixed(1) ?? "—"],
        ["Pts allowed", s?.points_allowed?.toFixed(1) ?? "—"],
      ],
      href: `/defenses?week=${week}&sel=${p.espn_player_id}`,
    };
  });
  return (
    <figure className={styles.figure}>
      <InteractiveScatter
        points={points}
        xLabel="Opponent implied total"
        yLabel="Sacks + takeaways"
        xFormat="num1"
        yFormat="num1"
        xDomain={domain(usable.map((u) => u.x), 1)}
        yDomain={domain(usable.map((u) => u.y), 0.3)}
        refX={[{ value: midX, label: `Median ${midX.toFixed(1)}` }]}
        refY={[{ value: midY, label: `Median ${midY.toFixed(1)}` }]}
        corners={{ tl: "Smash spot", tr: "Volatile", bl: "Quiet, low risk", br: "Avoid" }}
        ariaLabel={`Week ${week} D/ST matchups: opponent implied total against expected sacks plus takeaways, bubble size is expected points`}
        emptyText="No defenses with a line this week"
      />
      <ul className={styles.legend}>
        <li><svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><circle cx="7" cy="7" r="5" fill={FREE_AGENT} /></svg>Free agent</li>
        <li><svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><circle cx="7" cy="7" r="5" fill={ROSTERED} /></svg>Rostered</li>
        <li>Bigger bubble = more expected points</li>
      </ul>
      <figcaption className={styles.caption}>
        Left is a weaker opponent (fewer points expected from the betting line); up is more expected sacks and takeaways.
        The top-left quarter is where streamers live. Dashed lines are this week&apos;s medians. Select a bubble to highlight its row.
      </figcaption>
    </figure>
  );
}
