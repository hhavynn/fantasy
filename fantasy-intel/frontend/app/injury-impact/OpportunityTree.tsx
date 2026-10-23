import Link from "@/components/LeagueLink";
import { Badge, Notice } from "@/components/ui";
import type { ApiResult } from "@/lib/api";
import { Missing, num } from "@/lib/format";
import { gameLabel, levelBadge } from "@/lib/injury-impact";
import {
  CERTAINTY_LABEL, claimText, eligibilityHref, evidenceText, OUTCOME_LABEL, personnelText, REASON_LABEL, sampleText,
  SCENARIO_LABEL, sharePct, teamSummary, unitShort, type Effect, type OpportunityTreeResponse, type Peer, type Scenario,
  type TreePerson, type TreeTeam,
} from "@/lib/opportunity-tree";
import { plural } from "@/lib/plural";
import { withTeam } from "@/lib/team-perspective";
import { Kind, Who, WithWithout } from "./ImpactParts";
import styles from "./injury-impact.module.css";

const NOTICE_TITLE: Record<string, string> = {
  no_schedule: "Schedule not synced",
  no_depth_charts: "No depth charts synced",
  waiver_status_unknown: "Claim status unknown",
};

type Ctx = { team: string | null; prov: string; routes: string[] };

/** Status in words: a designation badge, "No designation", or a dash when no source covers him. */
function StatusCell({ p, prov }: { p: TreePerson; prov: string }) {
  const a = p.availability;
  if (!a.known) return <Missing why={`No ${prov} status and no NFL report entry: availability unknown`} />;
  if (a.level === "AVAILABLE") return <span className={styles.subLine}>No designation</span>;
  return <Badge value={levelBadge(a.level)} title={a.sources.join(", ") || undefined} />;
}

function ClaimCell({ p, ctx }: { p: TreePerson; ctx: Ctx }) {
  const c = claimText(p.ownership, p.roster, ctx.prov);
  return <Badge value={c.text} tone={c.tone} title={c.detail} />;
}

/** Cross-links: Start/Sit for your own player, the add/drop eligibility check for a claimable one, nothing otherwise. */
function Actions({ p, ctx }: { p: TreePerson; ctx: Ctx }) {
  if (p.ownership.claim === "yours") return <Link href={withTeam("/start-sit", ctx.team)}>Start/Sit</Link>;
  const href = eligibilityHref(p);
  if (href) return <Link href={withTeam(href, ctx.team)}>Check claim</Link>;
  if (p.ownership.claim === "opponent" || p.ownership.claim === "rostered") return <span className={styles.subLine}>Not claimable</span>;
  return <Missing why="Roster status in this league is unknown, so no claim is suggested" />;
}

function DepthTable({ group, t, ctx }: { group: TreeTeam["groups"][number]; t: TreeTeam; ctx: Ctx }) {
  const showRoutes = ctx.routes.length > 0;
  return (
    <div className={styles.depthGroup}>
      <p className={styles.treeCaption} id={`depth-${t.team}-${group.position}`}>
        {group.position} group, {group.ordered_by === "depth_chart" ? "in depth-chart order" : "by recent snap share (no depth chart synced)"}
        {group.more > 0 && <> · {group.more} more not shown</>}
      </p>
      <div className="table-wrap">
      <table className={styles.mates} aria-labelledby={`depth-${t.team}-${group.position}`}>
        <thead>
          <tr>
            <th scope="col">Player</th>
            <th scope="col" className="num" title="Rank at his position on the team's latest depth chart">Depth</th>
            <th scope="col">Status</th>
            <th scope="col" className="num" title="Snap share in the games he played among the team's last games">Snaps</th>
            {showRoutes && <th scope="col" className="num" title={`Route participation from ${ctx.routes.join(", ")}`}>Routes</th>}
            <th scope="col" className="num" title="His carries + targets over the team's skill-player carries + targets, in the games he played">Touch %</th>
            <th scope="col">Recent change</th>
            <th scope="col">Roster</th>
            <th scope="col">Next step</th>
          </tr>
        </thead>
        <tbody>
          {group.peers.map((p: Peer) => (
            <tr key={p.gsis_id} className={p.roster.is_mine ? "mine" : undefined}>
              <td className={styles.mateCell}>
                <Who id={p.espn_player_id} name={p.full_name} position={p.position}
                  meta={p.starter_in_scenario ? <span className={styles.subLine}>In a scenario below</span> : undefined} />
              </td>
              <td className="num">{p.depth_rank ?? <Missing why={t.depth_chart.known ? "Not on the latest depth chart" : "No depth chart synced for this team"} />}</td>
              <td><StatusCell p={p} prov={ctx.prov} /></td>
              <td className="num">
                {sharePct(p.shares.snap_share) ?? <Missing why="No snap counts in the team's last games" />}
                <span className={styles.games}>{p.shares.games_played}/{p.shares.team_games} g</span>
              </td>
              {showRoutes && <td className="num">{sharePct(p.shares.route_share) ?? <Missing why="No route data for his recent games" />}</td>}
              <td className="num">{sharePct(p.shares.touch_share) ?? <Missing why="Did not play in the team's last games" />}</td>
              <td>
                <div className={styles.personnel}>
                  {p.personnel.length === 0 ? <span className={styles.subLine}>None</span>
                    : p.personnel.map((c) => <span key={c.kind} className={styles.subLine}>{personnelText(c, t.team)}</span>)}
                </div>
              </td>
              <td><ClaimCell p={p} ctx={ctx} /></td>
              <td><Actions p={p} ctx={ctx} /></td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
    </div>
  );
}

function EffectRow({ e, s, ctx }: { e: Effect; s: Scenario; ctx: Ctx }) {
  const unit = unitShort(s.unit);
  return (
    <li className={styles.effect}>
      <Who id={e.espn_player_id} name={e.full_name} position={e.position} meta={<ClaimCell p={e} ctx={ctx} />} />
      <span className={styles.actionWhy}>
        <Kind kind={e.basis} /> {e.reasons.map((r) => REASON_LABEL[r]).join("; ")}
        {e.basis === "observed" && <> · {unit}/g with → without him{" "}
          <WithWithout withV={e.observed.with?.volume_per_game} withoutV={e.observed.without?.volume_per_game}
            withGames={e.observed.with?.games} withoutGames={e.observed.without?.games} /></>}
        {e.basis === "hypothesis" && e.estimate?.extra_per_game != null && e.reasons.includes("pro_rata_estimate")
          && <> · pro-rata +{e.estimate.extra_per_game.toFixed(1)} {unit}/g</>}
      </span>
      <span className={styles.actionWhy}><Actions p={e} ctx={ctx} /></span>
    </li>
  );
}

function ScenarioBlock({ s, ctx }: { s: Scenario; ctx: Ctx }) {
  const o = OUTCOME_LABEL[s.outcome];
  return (
    <article className={styles.scenario} aria-label={`${SCENARIO_LABEL[s.kind]}: ${s.subject.full_name}`}>
      <h4 className={styles.scenarioTitle}>
        <span>{SCENARIO_LABEL[s.kind]}</span>
        <Badge value={CERTAINTY_LABEL[s.certainty]} tone={s.certainty === "unconfirmed" ? "warn" : ""} />
        <Badge value={o.text} tone={o.tone} />
      </h4>
      <Who id={s.subject.espn_player_id} name={s.subject.full_name} position={s.subject.position}
        meta={s.subject.depth_rank !== null ? <span className={styles.subLine}>Depth {s.subject.position}{s.subject.depth_rank}</span> : undefined} />
      <p className={styles.scenarioFacts}>
        {s.evidence.length > 0 ? s.evidence.map(evidenceText).join(" · ") : "No status evidence this week"}
        <br />Sample: {sampleText(s)}.
      </p>
      {s.abstain ? (
        <p className={styles.none}><strong>Nobody named.</strong> {s.abstain.text}</p>
      ) : s.effects.length === 0 ? (
        <p className={styles.none}>
          {s.kind === "teammate_returning"
            ? `No teammate's ${s.unit_label} changed enough while he was out to name.`
            : `Over ${s.sample.team_games_without} ${plural(s.sample.team_games_without, "game")} without him no teammate gained enough to name: the volume spread out.`}
        </p>
      ) : (
        <ul className={styles.actionList}>{s.effects.map((e) => <EffectRow key={e.gsis_id} e={e} s={s} ctx={ctx} />)}</ul>
      )}
    </article>
  );
}

function TeamBlock({ t, ctx }: { t: TreeTeam; ctx: Ctx }) {
  const sum = teamSummary(t);
  const game = gameLabel(t.game);
  return (
    <details className={styles.group} data-team={t.team}>
      <summary>
        {t.team}{game ? <span className={styles.count}>{game}</span> : null}
        {t.affects_my_roster && <Badge value="Your roster" tone="info" />}
        <span className={styles.summaryNote}>{sum.scenarios} · {sum.outcome}</span>
      </summary>
      <div className={styles.treeBody}>
        <p className={styles.subLine}>
          {t.depth_chart.known ? <>Depth chart as of {t.depth_chart.as_of?.slice(0, 10)}.</> : <>No depth chart synced for {t.team}.</>}
          {t.coverage.status !== "complete" && t.coverage.reason ? <> {t.coverage.reason}</> : null}
        </p>
        {t.groups.map((g) => <DepthTable key={g.position} group={g} t={t} ctx={ctx} />)}
        <div className={styles.scenarios}>{t.scenarios.map((s) => <ScenarioBlock key={`${s.kind}-${s.subject.gsis_id}`} s={s} ctx={ctx} />)}</div>
      </div>
    </details>
  );
}

/**
 * The opportunity tree (issue #190): per NFL team with an injured or returning starter, a compact expandable depth
 * chart and one scenario per starter. Only observed relationships or labelled hypotheses are named; a scenario whose
 * inputs are unknown abstains. Streams inside a Suspense boundary beside the injury list.
 */
export async function OpportunityTree({ tree, team, prov }: {
  tree: Promise<ApiResult<OpportunityTreeResponse>>; team: string | null; prov: string;
}) {
  const res = await tree;
  if (!res.ok) {
    return <Notice tone="caution" title="Depth charts could not be loaded">{res.error}</Notice>;
  }
  const data = res.data;
  const c = data.counts;
  const ctx: Ctx = { team, prov, routes: data.sources.route_providers };
  return (
    <div className={styles.tree} data-testid="opportunity-tree">
      <p className={styles.treeLead}>
        {c.teams === 0
          ? <>No team has an injured or returning starter with a real role in week {data.week}.</>
          : <>{c.teams} {plural(c.teams, "team")}: {c.unavailable} unavailable and {c.returning} returning {plural(c.unavailable + c.returning, "starter")}.
            {" "}{c.supported} with an observed beneficiary, {c.hypothesis} with hypotheses only, {c.abstained} {plural(c.abstained, "abstains", "abstain")}.</>}
      </p>
      {data.notices.map((n) => <Notice key={n.code} tone={n.tone} title={NOTICE_TITLE[n.code] ?? "Data notice"}>{n.text}</Notice>)}
      {data.teams.map((t) => <TeamBlock key={t.team} t={t} ctx={ctx} />)}
      <p className={styles.subLine}>
        {data.method.observed} {data.method.hypothesis} {data.method.abstain}
        {" "}Usage through week {num(data.sources.usage_through_week, 0, "No usage synced")}.
        {" "}{data.sources.route_providers.length > 0 ? `Route participation: ${data.sources.route_providers.join(", ")}.` : "Route participation: unavailable."}
      </p>
    </div>
  );
}
