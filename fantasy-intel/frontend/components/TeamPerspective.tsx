import Link from "@/components/LeagueLink";
import type { ReactNode } from "react";
import { Notice } from "@/components/ui";
import { api } from "@/lib/api";
import { when } from "@/lib/format";
import { providerName } from "@/lib/labels";
import { getLeagueProfile } from "@/lib/league-profile.server";
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
export async function TeamPerspective({ path, sp, current, reset = TEAM_SPECIFIC_PARAMS, exclude = [], children }: {
  path: string;
  sp: SP;
  /** The team the page is showing (from its payload), or null when it shows none. */
  current: number | null;
  reset?: readonly string[];
  /** Teams not offered (e.g. the team a shared comparison is already against). */
  exclude?: readonly number[];
  /** The page's other header controls (week toggles), shown under the picker. */
  children?: ReactNode;
}) {
  const [res, profile] = await Promise.all([api<TeamsPayload>("/api/teams"), getLeagueProfile()]);
  const provider = providerName(profile.provider);
  if (!res.ok || res.data.teams.length === 0) return children ? <div className={styles.perspective}>{children}</div> : null;
  const { teams, default_team_id: def } = res.data;
  const team = teams.find((t) => t.team_id === current) ?? null;
  const options = teams.filter((t) => !exclude.includes(t.team_id)).map((t) => ({
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
              Viewing another team · read-only, nothing is sent to {provider} · <Link href={perspectiveHref(path, sp, null, reset)}>Back to default</Link>
              <span aria-hidden="true"> · </span>
            </span>
          )}
          {team.roster_players > 0
            ? <span title={`Roster as of the ${provider} sync ${when(team.roster_synced_at)}`}>Roster synced {when(team.roster_synced_at)} · {team.roster_players} players</span>
            : <>No roster in the latest {provider} sync</>}
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


/**
 * The backend's answer when a page needs a team and the visitor has none: an anonymous visitor to a public league
 * on the multi-league site (the classic site always had a team marked as yours).
 */
export function needsTeam(res: { ok: boolean; status?: number | null; error?: string | null }): boolean {
  return !res.ok && (res.status === 404 || res.status === 422) && /No team is marked as yours/.test(res.error ?? "");
}

/** In place of an error: the team picker and what picking one shows. */
export async function ChooseTeam({ path, sp, what }: { path: string; sp: SP; what: string }) {
  return (
    <>
      <TeamPerspective path={path} sp={sp} current={null} />
      <Notice tone="info" title="Pick a team">Pick a team above to see {what}.</Notice>
    </>
  );
}
