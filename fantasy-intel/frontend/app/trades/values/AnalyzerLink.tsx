"use client";

import { TeamLink } from "@/components/TeamLink";
import { VisuallyHidden } from "@/components/ui/VisuallyHidden";
import { useTeam } from "@/components/useTeam";
import { analyzerHref, analyzerLabel, type ValueRow } from "@/lib/trade-values";

/**
 * The trade analyzer with this player on the analyzed team's side: the team in the URL (`?team=`) or the one last
 * analyzed in this tab (`useTeam`), else the viewer's own team. `TeamLink` keeps that team and the page's league
 * (`/l/<lg>/trades?...&team=`), so the analyzer opens for the same team and league the board was opened from.
 */
export function AnalyzerLink({ row, myTeamId, suffix = "", className }: {
  row: Pick<ValueRow, "espn_player_id" | "team_id" | "name">;
  myTeamId: number | null;
  /** Visible text after the label, e.g. " in the analyzer". */
  suffix?: string;
  className?: string;
}) {
  const team = useTeam();
  const anchor = team !== null ? Number(team) : myTeamId;
  return (
    <TeamLink className={className} href={analyzerHref(row, anchor)} prefetch={false}>
      {analyzerLabel(row, anchor)}{suffix}
      {!suffix && <VisuallyHidden> {row.name ?? `player ${row.espn_player_id}`}</VisuallyHidden>}
    </TeamLink>
  );
}
