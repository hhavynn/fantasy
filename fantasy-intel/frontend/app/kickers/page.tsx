import { BackendProblem } from "@/components/BackendProblem";
import { TeamLink } from "@/components/TeamLink";
import { Badge, EmptyState, Method, Notice, PageHeader, Section } from "@/components/ui";
import { api } from "@/lib/api";
import { Missing, num, when } from "@/lib/format";
import { ESPN_COMPARISON_UNAVAILABLE } from "@/lib/labels";
import { getLeagueProfile } from "@/lib/league-profile.server";
import { currentLeague } from "@/lib/league-scope.server";
import { DISTANCES, conditions, factorChips, outlook, usesBettingLine, rowFlags, splitPlayers, statusText, type KickerPlayer, type KickerResponse } from "@/lib/kickers";
import styles from "./kickers.module.css";

export const metadata = { title: "Kicker outlook" };

const TITLE = "Kicker outlook";

type SP = Record<string, string | string[] | undefined>;
const one = (v: string | string[] | undefined): string | undefined => (Array.isArray(v) ? v[0] : v);

/** `espn` is false for a league whose provider has no projections (Sleeper): no ESPN column to compare with. */
function Row({ p, rank, espn }: { p: KickerPlayer; rank: number; espn: boolean }) {
  const i = p.independent;
  // A run can store an empty stat line or context ({}) for a kicker it projected without detail: treat it as missing.
  const s = i.stat_line && typeof i.stat_line.fg_made === "number" ? i.stat_line : null;
  const c = conditions(i.context && i.context.venue ? i.context : null);
  const chips = factorChips(p);
  return (
    <tr>
      <td className={styles.num}>{rank}</td>
      <td>
        <b>{p.name ?? `Player ${p.espn_player_id}`}</b>{" "}
        {p.injury_status && p.injury_status !== "ACTIVE" ? <Badge value={p.injury_status} /> : null}
        <span className={styles.sub}>{p.nfl_team} {p.is_home ? "vs" : "@"} {p.opponent}{p.fantasy_team_id ? "" : " · free agent"}</span>
      </td>
      <td className={styles.num}>
        <b>{num(i.expected_points, 1)}</b>
        <span className={styles.sub} title="80% empirical interval of the conditional projection">
          {i.interval_80 ? `${i.interval_80.low.toFixed(1)}–${i.interval_80.high.toFixed(1)}` : <Missing why="No calibrated interval" />}
        </span>
      </td>
      {espn && <td className={styles.num}>{num(p.espn.projected_points, 1)}</td>}
      <td className={styles.num}>{s ? `${s.fg_made.toFixed(1)} / ${s.fg_att.toFixed(1)}` : <Missing />}</td>
      {DISTANCES.map((d) => (
        <td key={d.key} className={styles.num} title={s ? `${Math.round(s.p_make_by_distance[d.key] * 100)}% make chance` : undefined}>
          {s ? s.fg_made_by_distance[d.key].toFixed(1) : <Missing />}
        </td>
      ))}
      <td className={styles.num}>{s ? `${s.pat_made.toFixed(1)} / ${s.pat_att.toFixed(1)}` : <Missing />}</td>
      <td className={styles.cond} title={c.detail ?? undefined}>{c.text}</td>
      <td className={styles.why}>
        {outlook(p) ?? ""}
        {chips.length > 0 && (
          <ul className={styles.chips} aria-label="Points vs a league-average kicker">
            {chips.map((c) => (
              <li key={c.feature} className={c.points > 0 ? styles.up : styles.down} title={c.text}>
                {c.label} <b>{c.points > 0 ? "+" : "−"}{Math.abs(c.points).toFixed(1)}</b>
              </li>
            ))}
          </ul>
        )}
        {rowFlags(i.flags).length > 0 && <span className={styles.sub}>{rowFlags(i.flags).join("; ")}</span>}
      </td>
    </tr>
  );
}

export default async function KickersPage({ searchParams }: PageProps<"/kickers">) {
  const sp: SP = await searchParams;
  const week = Number(one(sp.week)) || undefined;
  const [res, profile, league] = await Promise.all([
    api<KickerResponse>(`/api/projections/kickers${week ? `?week=${week}` : ""}`), getLeagueProfile(), currentLeague()]);
  const espn = profile.providerProjections;
  if (!res.ok) {
    return (
      <div className="stack">
        <PageHeader eyebrow="Waivers · Streaming" title={TITLE} />
        <BackendProblem error={res.error} kind={res.kind} status={res.status} />
      </div>
    );
  }
  const d = res.data;
  const weekForm = (
    <form action="/kickers" className={styles.weekForm}>
      <label>Week <input type="number" name="week" min={1} max={18} defaultValue={d.week} style={{ width: "4.5em" }} /></label>
      <button type="submit">Show</button>
      <TeamLink href="/schedule-intelligence#kdst-streaming" league={league?.id ?? null}>Multi-week streaming plan</TeamLink>
    </form>
  );
  const line = usesBettingLine(d.governance.model_version);
  const meta = (
    <>
      {d.governance.model_version} · week {d.week} {d.league.season}. Expected points from opportunity (FG and PAT tries
      {line ? ", including the betting line" : ""}) × conversion (distance, kicker accuracy, venue), scored with this league&apos;s
      kicker rules.
    </>
  );
  if (!d.available || !d.run) {
    return (
      <div className="stack">
        <PageHeader eyebrow="Waivers · Streaming" title={TITLE} meta={meta} actions={weekForm} />
        <Section title="No kicker run for this week">
          <EmptyState title={espn ? "ESPN's kicker projections are shown elsewhere" : "No kicker projections this week"}>{d.unavailable_reason}</EmptyState>
        </Section>
      </div>
    );
  }
  const { projected, other } = splitPlayers(d.players);
  return (
    <div className="stack">
      <PageHeader eyebrow="Waivers · Streaming" title={TITLE} meta={meta} actions={weekForm} />
      <Notice tone="info" title="This week's run">
        Run {d.run.run_id} made {when(d.run.created_at)} from data through{" "}
        {d.run.feature_cutoff.data_through_season} week {d.run.feature_cutoff.data_through_week}
        {d.run.artifact_verified ? "" : ` (artifact approval unverified: ${d.run.artifact_note ?? "unknown"})`}.
      </Notice>
      {d.run.forecast_horizon > 0 && (
        <Notice tone="info" title="Rest-of-season forecast">
          Made {d.run.forecast_horizon} week(s) ahead, before that week&apos;s injury report and weather are out.
        </Notice>
      )}
      {!espn && <Notice tone="info" title="No projection comparison for this league">{ESPN_COMPARISON_UNAVAILABLE}</Notice>}
      <Section title={`Projected kickers (${projected.length})`}
        subtitle="Points are expected points (chance he kicks × points if he does). FG makes by distance: 0–39 / 40–49 / 50–59 / 60+. Chips show how many points each factor adds or takes away against a league-average kicker; hover one for the detail." flush>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th className={styles.num}>#</th><th>Kicker</th><th className={styles.num}>Ours</th>
                {espn && <th className={styles.num}>ESPN</th>}<th className={styles.num}>FG made / tries</th>
                {DISTANCES.map((x) => <th key={x.key} className={styles.num}>{x.label}</th>)}
                <th className={styles.num}>PAT</th><th>Venue · weather</th><th>Why</th>
              </tr>
            </thead>
            <tbody>{projected.map((p, n) => <Row key={p.espn_player_id} p={p} rank={n + 1} espn={espn} />)}</tbody>
          </table>
        </div>
      </Section>
      {other.length > 0 && (
        <Section title={`Not projected (${other.length})`} subtitle={espn ? "Byes and unavailable kickers count as 0; the rest keep ESPN's number." : "Byes and unavailable kickers count as 0; the rest have no projection."} flush>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Kicker</th><th>Team</th><th>Status</th><th>Why</th></tr></thead>
              <tbody>
                {other.map((p) => (
                  <tr key={p.espn_player_id}>
                    <td>{p.name ?? p.espn_player_id}</td><td>{p.nfl_team ?? <Missing />}</td>
                    <td>{statusText(p.independent.coverage_status)}</td>
                    <td>{p.independent.missing_inputs.join("; ") || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}
      <Method title="How kicker projections work">
        <p>
          {line ? (
            <>
              Expected FG tries come from the league rate adjusted by the team&apos;s implied points from the betting line (other
              team and opponent stats did not improve validation accuracy for FG volume); expected PAT tries come from the
              team&apos;s touchdown rate and the opponent&apos;s, shrunk toward the league, plus the same implied total. A game
              without a posted line uses a league-average implied total.
            </>
          ) : (
            <>
              Expected FG tries come from the league rate (team and opponent context did not improve validation accuracy for FG
              volume); expected PAT tries come from the team&apos;s touchdown rate and the opponent&apos;s, shrunk toward the
              league. This week was projected by {d.governance.model_version}, before the betting line became an input.
            </>
          )}{" "}
          Each FG try&apos;s
          distance follows the league distribution tilted by the kicker&apos;s own long-range share, and its make chance is a smooth
          distance curve plus venue terms plus the kicker&apos;s accuracy, estimated from every earlier kick (misses and blocks
          included) and shrunk toward league average by sample size. Weather forecasts (NWS) are shown with their retrieval time;
          they are not a model input in {d.governance.model_version} because historical tests showed no reliable gain.
        </p>
        <ul>{d.notes.map((n) => <li key={n}>{n}</li>)}</ul>
      </Method>
    </div>
  );
}
