import Link from "@/components/LeagueLink";
import { Suspense, type ReactNode } from "react";
import { BackendProblem } from "@/components/BackendProblem";
import { TeamPerspective, UnknownTeam } from "@/components/TeamPerspective";
import {
  EmptyState, Glossary, Method, Notice, PageHeader, Section, Skeleton, Tabs,
} from "@/components/ui";
import { api, type ApiResult } from "@/lib/api";
import { when } from "@/lib/format";
import { providerName } from "@/lib/labels";
import { getLeagueProfile } from "@/lib/league-profile.server";
import {
  filterEntries, groupEntries, injuryHref, INJURY_POSITIONS, parseInjuryFilters, type InjuryEntry,
  type InjuryImpactResponse, type InjuryNotice,
} from "@/lib/injury-impact";
import type { OpportunityTreeResponse } from "@/lib/opportunity-tree";
import { plural } from "@/lib/plural";
import type { SearchParams } from "@/lib/sort";
import { isUnknownTeam, readTeamSelection, selectedTeamId, teamParamFrom, withTeamId } from "@/lib/team-perspective";
import { ImpactEntry } from "./ImpactEntry";
import { OpportunityTree } from "./OpportunityTree";
import { RosterActions } from "./RosterActions";
import styles from "./injury-impact.module.css";

export const metadata = { title: "Injury impact" };

const PATH = "/injury-impact";
const QUESTION = "Who is out or questionable this week, and whose role could grow if they sit?";
const STATUSES = [
  { value: "all", label: "All" }, { value: "out", label: "Out or doubtful" }, { value: "questionable", label: "Questionable" },
] as const;

const NOTICE_TITLE: Record<string, string> = {
  no_nfl_report: "No NFL injury report synced",
  report_not_published: "This week's NFL injury report isn't synced yet",
  stale_teams: "Some injury evidence is stale",
  missing_teams: "Some teams have no injury evidence",
  espn_stale: "ESPN statuses are old",
  no_espn_players: "No ESPN players synced",
  no_schedule: "Schedule not synced",
  no_usage: "No usage history",
  no_rosters: "League rosters not synced",
};

/** The notice titles that name the league's provider ("ESPN" unless the league is from elsewhere). */
function noticeTitle(code: string, prov: string): string {
  if (code === "espn_stale") return `${prov} statuses are old`;
  if (code === "no_espn_players") return `No ${prov} players synced`;
  return NOTICE_TITLE[code] ?? "Data notice";
}

function DataNotices({ notices, prov }: { notices: InjuryNotice[]; prov: string }) {
  if (notices.length === 0) return null;
  return (
    <div className={styles.notices}>
      {notices.map((n) => (
        <Notice key={n.code} tone={n.tone} title={noticeTitle(n.code, prov)}>{n.text}</Notice>
      ))}
    </div>
  );
}

function EntryList({ entries, team, labels }: { entries: InjuryEntry[]; team: string | null; labels: { prov: string; espn: boolean } }) {
  return <div className={styles.entries}>{entries.map((e) => <ImpactEntry key={e.gsis_id} e={e} team={team} labels={labels} />)}</div>;
}

function Shell({ sp, current, week, children }: { sp: SearchParams; current: number | null; week?: number; children: ReactNode }) {
  return (
    <div className="stack">
      <PageHeader eyebrow={week ? `My team · Week ${week}` : "My team"} title="Injury impact" meta={QUESTION}
        actions={<TeamPerspective path={PATH} sp={sp} current={current} />} />
      {children}
    </div>
  );
}

export default async function InjuryImpactPage({ searchParams }: PageProps<"/injury-impact">) {
  const sp = (await searchParams) as SearchParams;
  const sel = readTeamSelection(sp);
  if (sel.kind === "invalid") return <Shell sp={sp} current={null}><UnknownTeam path={PATH} sel={sel} /></Shell>;
  const filters = parseInjuryFilters(sp);
  // The opportunity tree is a second, slower read: started now, rendered in its own Suspense boundary below.
  // An early return below never awaits it, so a failure must never become an unhandled rejection.
  const tree = api<OpportunityTreeResponse>(withTeamId("/api/injury-impact/opportunity-tree", sel))
    .catch((): ApiResult<OpportunityTreeResponse> => ({ ok: false, data: null, error: "Request failed.", kind: "unknown" }));
  const [res, profile] = await Promise.all([api<InjuryImpactResponse>(withTeamId("/api/injury-impact", sel)), getLeagueProfile()]);
  const prov = providerName(profile.provider);
  const labels = { prov, espn: profile.providerProjections };

  if (isUnknownTeam(res, sel)) {
    return <Shell sp={sp} current={null}><UnknownTeam path={PATH} sel={sel} detail={res.ok ? null : res.error} /></Shell>;
  }
  if (!res.ok) {
    const notSynced = res.status === 503 && res.kind === "http" && !/no database yet/i.test(res.error);
    return (
      <Shell sp={sp} current={selectedTeamId(sel)}>
        {notSynced ? (
          <Section>
            <EmptyState title="Your league isn't synced yet" action={<Link href="/data">Check data status</Link>}>
              <p>The injury view needs your league&apos;s current week, {prov} players and the NFL usage history. Run the {prov} and NFL syncs, then reload this page.</p>
              <p className="faint">{res.error}</p>
            </EmptyState>
          </Section>
        ) : <BackendProblem error={res.error} kind={res.kind} status={res.status} />}
      </Shell>
    );
  }

  const data = res.data;
  const f = data.freshness;
  const shown = filterEntries(data.players, filters);
  const groups = groupEntries(shown);
  const filtered = filters.pos !== "ALL" || filters.status !== "all";
  const posHref = (p: string) => injuryHref(sp, { pos: p === "ALL" ? null : p });
  const fresh = f.summary.by_status;
  const report = f.injury_report;
  const team = teamParamFrom(sp);

  return (
    <Shell sp={sp} current={data.league_context.my_team_id} week={data.week}>
      <div className={styles.sourceLine}>
        <p>
          <strong>Week {data.week}.</strong>{" "}
          {report.available && report.current_week_rows > 0
            ? <>NFL injury report for week {data.week} synced</>
            : <>No week {data.week} NFL report yet</>}
          {" "}· {prov} statuses {when(f.espn_players_synced_at)} · usage through week {f.usage_through_week ?? "—"}
          {f.depth_chart_as_of && <> · depth charts {when(f.depth_chart_as_of)}</>}.
        </p>
        <p className="faint">
          {f.summary.teams > 0
            ? <>Injury evidence: {fresh.fresh} {plural(fresh.fresh, "team")} fresh{fresh.stale ? `, ${fresh.stale} stale` : ""}
              {fresh.missing ? `, ${fresh.missing} missing` : ""}{fresh.post_kickoff_only ? `, ${fresh.post_kickoff_only} already kicked off` : ""} of {f.summary.teams} on the slate
              (stale = retrieved more than {f.thresholds.stale_after_hours} hours before kickoff).</>
            : <>No games are scheduled this week, so per-team injury freshness can&apos;t be checked.</>}
        </p>
      </div>

      <DataNotices notices={data.notices} prov={prov} />

      <Notice tone="info" title="Observed history first, estimates labelled">
        Workloads and with/without splits are counted from this season&apos;s games. The pro-rata share (dashed) is a quick estimate of
        who absorbs the vacated volume; read it beside the observed games.
        {labels.espn && <>{" "}ESPN numbers are ESPN&apos;s own weekly projection.</>}
      </Notice>

      {data.league_context.available && (
        <Section title="For your roster" subtitle="Games still to play this week; long-term absences are left out because their volume is already redistributed.">
          <RosterActions entries={data.players} team={team} />
        </Section>
      )}

      <Section title="Depth charts and opportunity"
        subtitle="Teams with an injured or returning starter. A teammate is named only when observed games support it, or as a labelled hypothesis; otherwise the scenario abstains.">
        <Suspense fallback={<Skeleton w="100%" h={120} radius={8} />}>
          <OpportunityTree tree={tree} team={team} prov={prov} />
        </Suspense>
      </Section>

      <div className={styles.controls}>
        <Tabs label="Position" items={INJURY_POSITIONS.map((p) => ({ href: posHref(p), label: p === "ALL" ? "All" : p }))}
          current={posHref(filters.pos)} />
        <div className={styles.chipRows}>
          <nav className="filters" aria-label="Injury status">
            <span className={styles.chipLabel} aria-hidden="true">Status</span>
            {STATUSES.map((o) => (
              <Link key={o.value} className={`chip ${filters.status === o.value ? "active" : ""}`} scroll={false}
                aria-current={filters.status === o.value ? "page" : undefined}
                href={injuryHref(sp, { status: o.value === "all" ? null : o.value })}>{o.label}</Link>
            ))}
          </nav>
          {filtered && <Link className="chip reset" href={injuryHref(sp, { pos: null, status: null })}>Clear filters</Link>}
        </div>
      </div>

      {data.counts.total === 0 ? (
        <Section>
          <EmptyState title="No meaningful injuries this week" action={<Link href="/data">Check data status</Link>}>
            <p>No QB, RB, WR or TE with a real role this season has a questionable, doubtful, out, IR or suspended status for week {data.week}.
              {data.notices.length > 0 ? " Check the data notices above: a missing report is not a clean bill of health." : ""}</p>
          </EmptyState>
        </Section>
      ) : shown.length === 0 ? (
        <Section>
          <EmptyState title="No injuries match these filters" action={<Link className="chip reset" href={injuryHref(sp, { pos: null, status: null })}>Clear filters</Link>}>
            Try another position or status. {data.counts.total} {plural(data.counts.total, "player")} {plural(data.counts.total, "is", "are")} listed in total.
          </EmptyState>
        </Section>
      ) : (
        <>
          <Section title={<>This week <span className={styles.count}>{groups.upcoming.length} {plural(groups.upcoming.length, "player")}</span></>}
            subtitle="Games still to play, most severe and biggest role first.">
            {groups.upcoming.length > 0 ? <EntryList entries={groups.upcoming} team={team} labels={labels} />
              : <p className={styles.emptyGroup}>No absence in this view has a game still to play.</p>}
          </Section>
          {groups.longTerm.length > 0 && (
            <details className={styles.group}>
              <summary>
                Long-term absences <span className={styles.count}>{groups.longTerm.length}</span>
                <span className={styles.summaryNote}>Out {data.method.thresholds.long_term_missed}+ straight games; teammates&apos; recent usage already reflects it</span>
              </summary>
              <EntryList entries={groups.longTerm} team={team} labels={labels} />
            </details>
          )}
          {groups.done.length > 0 && (
            <details className={styles.group}>
              <summary>
                Already kicked off or on a bye <span className={styles.count}>{groups.done.length}</span>
                <span className={styles.summaryNote}>Status no longer affects this week</span>
              </summary>
              <EntryList entries={groups.done} team={team} labels={labels} />
            </details>
          )}
        </>
      )}

      <Glossary terms={["Q / D / O", "IR", "Target share", "Snap share", "—"]} />
      <Method title="How injury impact is calculated">
        <div className={styles.method}>
          <p><b>Who is listed.</b> Every QB, RB, WR and TE whose status this week is questionable, doubtful, out, on injured reserve or suspended,
            taking the more severe of {prov}&apos;s status and the week&apos;s NFL injury report. He must have played for his current team this season
            with a real role (at least {Math.round(data.method.thresholds.min_snap_share * 100)}% of snaps or {data.method.thresholds.min_opportunities_per_game} carries
            plus targets a game; quarterbacks {Math.round(data.method.thresholds.min_qb_snap_share * 100)}% of snaps), or be on a fantasy roster in your league.
            {data.counts.skipped.no_games + data.counts.skipped.small_role > 0 && <> Left out this week: {data.counts.skipped.no_games} without a game this season and {data.counts.skipped.small_role} with a small role.</>}</p>
          <p><b>Observed.</b> {data.method.observed} Workload is his average over the games he played for the team; the sparkline leaves gaps for
            games he missed. A teammate&apos;s &quot;with → without&quot; is his volume per game in team games the injured player played versus missed, with the games behind each side.</p>
          <p><b>Estimate.</b> {data.method.estimate} The split uses each healthy teammate&apos;s volume in the last {data.method.thresholds.pool_weeks} games
            the injured player played, so volume a teammate already absorbed is not counted twice. Receivers split targets (WR, TE and RB teammates);
            running backs split carries plus targets among running backs. A quarterback absence shows the next quarterback on the depth chart and how
            his pass catchers did without the starter, with no split.</p>
          <p><b>Freshness.</b> Each team&apos;s injury evidence is checked against its kickoff: fresh, stale (retrieved more than {f.thresholds.stale_after_hours} hours
            before kickoff with the game within {f.thresholds.window_hours} hours), missing, or retrieved only after kickoff. Missing evidence is never read as healthy.
            The teammate-context research model is not used on this page.</p>
        </div>
      </Method>
    </Shell>
  );
}
