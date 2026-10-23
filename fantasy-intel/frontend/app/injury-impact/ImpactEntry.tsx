import Link from "@/components/LeagueLink";
import { CompareLink } from "@/components/CompareLink";
import { Badge, Sparkline } from "@/components/ui";
import { Missing, num, pct } from "@/lib/format";
import { withTeam } from "@/lib/team-perspective";
import {
  absenceLabel, CLARITY_LABEL, compareTarget, freshnessLabel, gameLabel, levelBadge, levelLabel, snapSeries,
  waiversHref, type InjuryEntry, type Receiver, type Teammate,
} from "@/lib/injury-impact";
import { Change, Kind, RosterBadge, ShareBar, Who, WithWithout } from "./ImpactParts";
import styles from "./injury-impact.module.css";

const UNIT_SHORT = { touches: "touches", targets: "targets", pass_attempts: "att" } as const;

function kickoffText(iso: string | null): string | null {
  if (!iso) return null;
  return new Date(iso).toLocaleString("en-US", { weekday: "short", hour: "numeric", minute: "2-digit", timeZone: "America/New_York" }) + " ET";
}

/** `prov` is the league's provider ("ESPN" or "Sleeper"); `espn` is false where it publishes no projections. */
type Labels = { prov: string; espn: boolean };

function StatusLine({ s, prov }: { s: InjuryEntry["status"]; prov: string }) {
  const parts = [levelLabel(s.level), s.injury].filter(Boolean).join(" · ");
  const src = [s.nfl_report_status ? `NFL report: ${s.nfl_report_status}` : null,
    s.espn_injury_status && s.espn_injury_status !== "ACTIVE" ? `${prov}: ${s.espn_injury_status.toLowerCase().replaceAll("_", " ")}` : null]
    .filter(Boolean).join(" · ");
  return (
    <>
      <span className={styles.statusText}>{parts}</span>
      {(src || s.practice_status) && <span className={styles.subLine}>{[src, s.practice_status].filter(Boolean).join(" · ")}</span>}
    </>
  );
}

function Workload({ e }: { e: InjuryEntry }) {
  const w = e.workload;
  const isQb = e.position === "QB";
  const weeks = w.weekly.map((p) => p.week);
  return (
    <div className={styles.workload}>
      <h3 className={styles.blockTitle}>Workload <Kind kind="observed" /></h3>
      <dl className={styles.facts}>
        <div><dt>Snaps</dt><dd>{pct(w.snap_share, 0)}</dd></div>
        <div><dt>Last {w.recent_weeks.length}</dt><dd>{pct(w.recent_snap_share, 0)}</dd></div>
        {isQb
          ? <div><dt>Att/g</dt><dd>{num(w.pass_attempts_per_game, 1, "No passing stats synced")}</dd></div>
          : <div><dt>Tgt/g</dt><dd>{num(w.targets_per_game, 1)}</dd></div>}
        {(isQb || e.position === "RB") && <div><dt>Car/g</dt><dd>{num(w.carries_per_game, 1)}</dd></div>}
        {e.position === "RB" && <div><dt>Backfield</dt><dd>{pct(w.backfield_share, 0, "No designed-rush share")}</dd></div>}
        {(e.position === "WR" || e.position === "TE") && <div><dt>Tgt share</dt><dd>{pct(w.target_share, 0)}</dd></div>}
        <div><dt>xFP/g</dt><dd>{num(w.xfp_per_game, 1, "No expected points")}</dd></div>
        <div><dt>PPR/g</dt><dd>{num(w.ppr_per_game, 1)}</dd></div>
      </dl>
      <figure className={styles.spark}>
        <Sparkline points={snapSeries(w)} width={128} height={26} max={1} />
        <figcaption>
          Snap share, weeks {weeks[0]}–{weeks[weeks.length - 1]} ({w.games} of {w.team_games} games; gaps are games missed)
          {w.role_trend && w.role_trend !== "INSUFFICIENT" ? <> · role trend <Badge value={w.role_trend} /></> : null}
        </figcaption>
      </figure>
      <p className={styles.absence}>{absenceLabel(e.absence)}{e.absence.kind === "long_term" || e.absence.kind === "ongoing"
        ? ". Teammates' recent usage already includes the games without him." : "."}</p>
    </div>
  );
}

function TeammateTable({ e, espn }: { e: InjuryEntry; espn: boolean }) {
  const unit = UNIT_SHORT[e.impact.unit];
  const rows = e.impact.teammates;
  if (rows.length === 0) {
    return <p className={styles.none}>No teammate at this position has played for {e.team} this season or appears on its depth chart.</p>;
  }
  return (
    <div className="table-wrap">
      <table className={styles.mates}>
        <thead>
          <tr>
            <th scope="col">Teammate</th>
            <th scope="col" className="num" title={`${e.impact.unit_label} per game with him → without him, and the games behind each side`}>With → w/o</th>
            <th scope="col" className="num" title="Observed change per game without him">Change</th>
            <th scope="col" title={`Pro-rata share of his recent volume and the extra ${unit} per game it implies.`}>Pro-rata</th>
            <th scope="col" className="num" title={`${e.impact.unit_label} per game over the team's last 3 games`}>Recent/g</th>
            <th scope="col" className="num" title="Rank at his position on the team's latest depth chart">Depth</th>
            {espn && <th scope="col" className="num" title="ESPN's weekly projection (ESPN's model, league scoring)">ESPN</th>}
            <th scope="col">Roster</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((m: Teammate) => (
            <tr key={m.gsis_id} className={m.roster.is_mine ? "mine" : undefined}>
              <td className={styles.mateCell}>
                <Who id={m.espn_player_id} name={m.full_name} position={m.position}
                  meta={!m.healthy ? <Badge value={levelBadge(m.availability)} title="Also unavailable; left out of the split" /> : undefined} />
              </td>
              <td className="num">
                <WithWithout withV={m.observed.with?.volume_per_game} withoutV={m.observed.without?.volume_per_game}
                  withGames={m.observed.with?.games} withoutGames={m.observed.without?.games} />
              </td>
              <td className="num"><Change v={m.observed.delta_per_game} /></td>
              <td>{m.healthy
                ? <ShareBar share={m.estimate?.share ?? null} extra={m.estimate?.extra_per_game ?? null} unit={e.impact.unit_label} />
                : <span className={styles.subLine}>Also out</span>}</td>
              <td className="num">{num(m.recent_volume_per_game, 1)}</td>
              <td className="num">{m.depth_rank ?? <Missing why="Not on the latest depth chart" />}</td>
              {espn && <td className="num">{num(m.espn_projection, 1, "No ESPN projection this week")}</td>}
              <td><RosterBadge roster={m.roster} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function QbTables({ e, espn }: { e: InjuryEntry; espn: boolean }) {
  const qbs = e.impact.teammates;
  return (
    <>
      {qbs.length > 0 ? (
        <div className="table-wrap">
          <table className={styles.mates}>
            <thead>
              <tr>
                <th scope="col">Quarterback</th>
                <th scope="col" className="num" title="Rank on the team's latest depth chart">Depth</th>
                <th scope="col" className="num" title="Pass attempts per game in team games without the starter, and how many he played">Att/g without</th>
                {espn && <th scope="col" className="num" title="ESPN's weekly projection (ESPN's model, league scoring)">ESPN</th>}
                <th scope="col">Roster</th>
              </tr>
            </thead>
            <tbody>
              {qbs.map((m) => (
                <tr key={m.gsis_id} className={m.roster.is_mine ? "mine" : undefined}>
                  <td className={styles.mateCell}><Who id={m.espn_player_id} name={m.full_name} position={m.position}
                    meta={!m.healthy ? <Badge value={levelBadge(m.availability)} /> : undefined} /></td>
                  <td className="num">{m.depth_rank ?? <Missing why="Not on the latest depth chart" />}</td>
                  <td className="num">{m.observed.without
                    ? <>{num(m.observed.without.volume_per_game, 1)} <span className={styles.games}>{m.observed.without.games} g</span></>
                    : <Missing why="Has not played in a game without the starter this season" />}</td>
                  {espn && <td className="num">{num(m.espn_projection, 1, "No ESPN projection this week")}</td>}
                  <td><RosterBadge roster={m.roster} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <p className={styles.none}>No backup quarterback on the depth chart or in this season&apos;s usage.</p>}
      {e.impact.receivers.length > 0 && (
        <>
          <h4 className={styles.subTitle}>Pass catchers whose production depends on the quarterback</h4>
          <div className="table-wrap">
            <table className={styles.mates}>
              <thead>
                <tr>
                  <th scope="col">Pass catcher</th>
                  <th scope="col" className="num" title="Targets per game over the team's last 3 games">Recent tgt/g</th>
                  <th scope="col" className="num" title="PPR points per game with the starter → without him">PPR with → without</th>
                  <th scope="col" className="num">Change</th>
                  {espn && <th scope="col" className="num" title="ESPN's weekly projection (ESPN's model, league scoring)">ESPN</th>}
                  <th scope="col">Roster</th>
                </tr>
              </thead>
              <tbody>
                {e.impact.receivers.map((r: Receiver) => (
                  <tr key={r.gsis_id} className={r.roster.is_mine ? "mine" : undefined}>
                    <td className={styles.mateCell}><Who id={r.espn_player_id} name={r.full_name} position={r.position} /></td>
                    <td className="num">{num(r.recent_targets_per_game, 1)}</td>
                    <td className="num"><WithWithout withV={r.observed.with?.ppr_per_game} withoutV={r.observed.without?.ppr_per_game}
                      withGames={r.observed.with?.games} withoutGames={r.observed.without?.games} /></td>
                    <td className="num"><Change v={r.observed.ppr_delta_per_game} /></td>
                    {espn && <td className="num">{num(r.espn_projection, 1, "No ESPN projection this week")}</td>}
                    <td><RosterBadge roster={r.roster} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  );
}

/** One absence: who, status, game and data freshness; his observed workload; who could absorb it, and links. */
export function ImpactEntry({ e, team, labels = { prov: "ESPN", espn: true } }: { e: InjuryEntry; team: string | null; labels?: Labels }) {
  const fresh = freshnessLabel(e.freshness, labels.prov);
  const game = gameLabel(e.game);
  const ko = kickoffText(e.game.kickoff);
  const target = compareTarget(e);
  const isQb = e.position === "QB";
  const unitLabel = e.impact.unit_label;
  return (
    <article className={styles.entry} aria-labelledby={`inj-${e.gsis_id}`}>
      <header className={styles.entryHead}>
        <div className={styles.who} id={`inj-${e.gsis_id}`}>
          <Who id={e.espn_player_id} name={e.full_name} position={e.position} team={e.team}
            meta={<Badge value={levelBadge(e.status.level)} />} />
        </div>
        <dl className={styles.headFacts}>
          <div><dt>Status</dt><dd><StatusLine s={e.status} prov={labels.prov} /></dd></div>
          <div>
            <dt>Week {e.game.week}</dt>
            <dd>
              {game ?? <Missing why="Schedule not synced" />}
              {e.game.started ? <span className={styles.subLine}>Kicked off</span> : ko ? <span className={styles.subLine}>{ko}</span> : null}
            </dd>
          </div>
          <div>
            <dt>Injury data</dt>
            <dd>
              <Badge value={fresh.text} tone={fresh.tone} title={e.freshness?.reason.replaceAll("_", " ")} />
              {fresh.detail && <span className={styles.subLine}>{fresh.detail}</span>}
            </dd>
          </div>
          <div><dt>Roster</dt><dd><RosterBadge roster={e.roster} /></dd></div>
        </dl>
      </header>

      <div className={styles.entryBody}>
        <Workload e={e} />
        <div className={styles.absorb}>
          <h3 className={styles.blockTitle}>
            {isQb ? "Who starts if he sits" : <>Who could absorb {e.impact.vacated_per_game !== null ? `${e.impact.vacated_per_game.toFixed(1)} ` : ""}{unitLabel} per game</>}
            {" "}<Kind kind="observed" /> {!isQb && <Kind kind="estimate" />}
          </h3>
          <p className={styles.clarity}>
            <Badge value={CLARITY_LABEL[e.impact.clarity.label]} tone={e.impact.clarity.label === "leaning" ? "info" : "warn"} />
            <span>{e.impact.clarity.text}</span>
          </p>
          {isQb ? <QbTables e={e} espn={labels.espn} /> : <TeammateTable e={e} espn={labels.espn} />}
        </div>
      </div>

      <footer className={styles.links}>
        <Link href={withTeam("/start-sit", team)}>Start/Sit</Link>
        <Link href={withTeam(waiversHref(e.position), team)}>{e.position} waivers</Link>
        {e.espn_player_id !== null && target?.espn_player_id != null && (
          <CompareLink a={e.espn_player_id} b={target.espn_player_id} aName={e.full_name} bName={target.full_name}>
            Compare with {target.full_name}
          </CompareLink>
        )}
        {e.espn_player_id !== null && <Link href={`/players?id=${e.espn_player_id}`}>Player page</Link>}
      </footer>
    </article>
  );
}
