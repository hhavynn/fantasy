import { BackendProblem } from "@/components/BackendProblem";
import { EmptyState, Method, Notice, PageHeader, Section } from "@/components/ui";
import { api } from "@/lib/api";
import { headline, lineText, splitDefenses, statusText, takeaways, type DstPlayer, type DstResponse } from "@/lib/defenses";
import { Missing, num, when } from "@/lib/format";
import { DstChart } from "./DstChart";
import styles from "./defenses.module.css";

export const metadata = { title: "D/ST outlook" };

const TITLE = "D/ST outlook";

type SP = Record<string, string | string[] | undefined>;
const one = (v: string | string[] | undefined): string | undefined => (Array.isArray(v) ? v[0] : v);

function Row({ p, rank, selected }: { p: DstPlayer; rank: number; selected: boolean }) {
  const i = p.independent;
  const s = i.stat_line;
  const line = lineText(i.context);
  const td = s?.p_any_td;
  return (
    <tr id={selected ? "evidence" : `dst-${p.espn_player_id}`} className={selected ? styles.selected : undefined} aria-current={selected || undefined}>
      <td className={styles.num}>{rank}</td>
      <td>
        <b>{p.name ?? `D/ST ${p.espn_player_id}`}</b>
        <span className={styles.sub}>{p.nfl_team} {p.is_home ? "vs" : "@"} {p.opponent}{p.fantasy_team_id ? "" : " · free agent"}</span>
      </td>
      <td className={styles.num}>
        <b>{num(i.expected_points, 1)}</b>
        <span className={styles.sub} title="80% empirical interval">
          {i.interval_80 ? `${i.interval_80.low.toFixed(1)}–${i.interval_80.high.toFixed(1)}` : <Missing why="No calibrated interval" />}
        </span>
      </td>
      <td className={styles.num}>{num(p.espn.projected_points, 1)}</td>
      <td className={styles.num}>{num(i.context?.opponent_implied_total ?? null, 1)}</td>
      <td className={styles.num}>{num(s?.sacks ?? null, 1)}</td>
      <td className={styles.num}>{num(takeaways(s), 1)}</td>
      <td className={styles.num}>{num(s?.points_allowed ?? null, 1)}</td>
      <td className={styles.num}>{td === null || td === undefined ? <Missing /> : `${Math.round(td * 100)}%`}</td>
      <td className={styles.why}>
        {headline(p) ?? ""}
        {line && <span className={styles.sub}>{line}</span>}
        {i.flags.length > 0 && <span className={styles.sub}>{i.flags.join(" ")}</span>}
      </td>
    </tr>
  );
}

export default async function DefensesPage({ searchParams }: PageProps<"/defenses">) {
  const sp: SP = await searchParams;
  const week = Number(one(sp.week)) || undefined;
  const sel = Number(one(sp.sel)) || null;
  const res = await api<DstResponse>(`/api/projections/defenses${week ? `?week=${week}` : ""}`);
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
    <form action="/defenses" className={styles.weekForm}>
      <label>Week <input type="number" name="week" min={1} max={18} defaultValue={d.week} style={{ width: "4.5em" }} /></label>
      <button type="submit">Show</button>
    </form>
  );
  const meta = (
    <>
      {d.governance.model_version} · week {d.week} {d.league.season}. Expected points from each defense&apos;s sack, takeaway,
      points-allowed and yards-allowed rates against this opponent and the betting line, scored with this league&apos;s D/ST rules.
    </>
  );
  if (!d.available || !d.run) {
    return (
      <div className="stack">
        <PageHeader eyebrow="Waivers · Streaming" title={TITLE} meta={meta} actions={weekForm} />
        <Section title="No D/ST run for this week">
          <EmptyState title="ESPN's D/ST projections are shown elsewhere">{d.unavailable_reason}</EmptyState>
        </Section>
      </div>
    );
  }
  const { projected, other } = splitDefenses(d.players);
  return (
    <div className="stack">
      <PageHeader eyebrow="Waivers · Streaming" title={TITLE} meta={meta} actions={weekForm} />
      <Notice tone="info" title="This week's run">
        Run {d.run.run_id} made {when(d.run.created_at)} from games through {d.run.feature_cutoff.data_through_season} week{" "}
        {d.run.feature_cutoff.data_through_week}, with the betting lines as they stood then
        {d.run.artifact_verified ? "" : ` (artifact approval unverified: ${d.run.artifact_note ?? "unknown"})`}.
      </Notice>
      <Section title="Matchup map" subtitle="Weak opponent on the left, more splash plays up top, bubble size is expected points.">
        <DstChart players={projected} week={d.week} sel={sel} />
      </Section>
      <Section title={`Projected defenses (${projected.length})`}
        subtitle="Points are expected fantasy points under this league's D/ST scoring. Opp. total is the opponent's implied points from the line." flush>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th className={styles.num}>#</th><th>D/ST</th><th className={styles.num}>Ours</th>
                <th className={styles.num}>ESPN</th><th className={styles.num}>Opp. total</th>
                <th className={styles.num}>Sacks</th><th className={styles.num}>Takeaways</th>
                <th className={styles.num}>Pts allowed</th><th className={styles.num}>TD chance</th><th>Why</th>
              </tr>
            </thead>
            <tbody>{projected.map((p, n) => <Row key={p.espn_player_id} p={p} rank={n + 1} selected={p.espn_player_id === sel} />)}</tbody>
          </table>
        </div>
      </Section>
      {other.length > 0 && (
        <Section title={`Not projected (${other.length})`} subtitle="A bye counts as 0." flush>
          <div className="table-wrap">
            <table>
              <thead><tr><th>D/ST</th><th>Team</th><th>Status</th></tr></thead>
              <tbody>
                {other.map((p) => (
                  <tr key={p.espn_player_id}>
                    <td>{p.name ?? p.espn_player_id}</td><td>{p.nfl_team ?? <Missing />}</td>
                    <td>{statusText(p.independent.coverage_status)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}
      <Method title="How D/ST projections work">
        <p>
          Each defense gets ratings for sacks, interceptions, fumble recoveries, blocked kicks, safeties, return touchdowns, points
          allowed and yards allowed, built from recent games and adjusted for the offenses it faced; the opponent gets the matching
          offensive ratings. A model fitted on earlier seasons combines those ratings with the betting line (the opponent&apos;s
          implied total and the expected margin) into expected counts. Points- and yards-allowed tiers are scored as
          probabilities, so 13 vs 14 points allowed lands on the right side of a tier edge, and touchdowns are a small, steady
          expectation rather than a prediction. The coming week only: later weeks use ESPN&apos;s D/ST numbers.
        </p>
        <ul>{d.notes.map((n) => <li key={n}>{n}</li>)}</ul>
      </Method>
    </div>
  );
}
