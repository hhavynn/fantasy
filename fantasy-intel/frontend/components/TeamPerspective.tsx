import Link from "next/link";
import type { ReactNode } from "react";
import { Notice } from "@/components/ui";
import { api } from "@/lib/api";
import { when } from "@/lib/format";
import {
  perspectiveHref, teamOptionLabel, TEAM_SPECIFIC_PARAMS, type TeamSelection, type TeamsPayload,
} from "@/lib/team-perspective";
import { TeamPerspectiveSelect } from "./TeamPerspectiveSelect";
import styles from "./TeamPerspective.module.css";

type SP = Record<string, string | string[] | undefined>;

/**
 * "Analyzing: [team]" for a league-personalized page. Each option is a full URL (`?team=`), so picking a team
 * reloads the page and every number on it is recomputed for that roster; the link can be shared. The line under
 * it says whose roster this is and how fresh it is. Read-only: nothing here writes to ESPN or to the league data.
 */
export async function TeamPerspective({ path, sp, current, reset = TEAM_SPECIFIC_PARAMS, children }: {
  path: string;
  sp: SP;
  /** The team the page is showing (from its payload), or null when it shows none. */
  current: number | null;
  reset?: readonly string[];
  /** The page's other header controls (week toggles), shown under the picker. */
  children?: ReactNode;
}) {
  const res = await api<TeamsPayload>("/api/teams");
  if (!res.ok || res.data.teams.length === 0) return children ? <div className={styles.perspective}>{children}</div> : null;
  const { teams, default_team_id: def } = res.data;
  const team = teams.find((t) => t.team_id === current) ?? null;
  const options = teams.map((t) => ({
    value: perspectiveHref(path, sp, t.team_id === def ? null : t.team_id, reset),
    label: teamOptionLabel(t),
    selected: t.team_id === current,
  }));
  return (
    <div className={styles.perspective}>
      <TeamPerspectiveSelect options={options} placeholder={team ? null : "Pick a team"} />
      {team && (
        <p className={styles.fresh}>
          {team.team_id !== def && (
            <span className={styles.other}>
              Viewing another team · read-only, nothing is sent to ESPN · <Link href={perspectiveHref(path, sp, null, reset)}>Back to default</Link>
              <span aria-hidden="true"> · </span>
            </span>
          )}
          {team.roster_players > 0
            ? <span title={`Roster as of the ESPN sync ${when(team.roster_synced_at)}`}>Roster synced {when(team.roster_synced_at)} · {team.roster_players} players</span>
            : <>No roster in the latest ESPN sync</>}
        </p>
      )}
      {children}
    </div>
  );
}

/** A requested team that is not in the league (unknown, deleted, or from an old season's link). */
export function UnknownTeam({ path, sel, detail }: { path: string; sel: TeamSelection; detail?: string | null }) {
  const asked = sel.kind === "team" ? `Team ${sel.id}` : sel.kind === "invalid" ? `"${sel.raw}"` : "That team";
  return (
    <Notice tone="caution" title={`${asked} isn't a team in this league`}>
      {detail ? <>{detail} </> : null}The link may be from another season or a team that no longer exists.{" "}
      <Link href={path}>Show the default team</Link>.
    </Notice>
  );
}
