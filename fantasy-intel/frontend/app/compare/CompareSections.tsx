import Link from "next/link";
import { PlayerIdentity } from "@/components/PlayerIdentity";
import { Badge, Notice, Section, TrendArrow } from "@/components/ui";
import { cx } from "@/lib/cx";
import {
  type CompareCard, type CompareResponse, type Side, type WeeklyEntry,
  availabilityView, formatEvidence, leaderLabel, playerName,
} from "@/lib/compare";
import { label, num, when } from "@/lib/format";
import { hasInjuryDesignation, ownerBadge } from "../players/helpers";
import styles from "./compare.module.css";

const SIDES: Side[] = ["a", "b"];
const sideClass = (side: Side) => (side === "a" ? styles.sideA : styles.sideB);
const letter = (side: Side) => (side === "a" ? "A" : "B");

/** Two identity cards: who is on each side, with the facts that change how to read the numbers. */
export function CompareHero({ r, only }: { r: CompareResponse; only?: Side }) {
  return (
    <div className={cx(styles.hero, only && styles.heroOne)}>
      {SIDES.filter((side) => !only || side === only).map((side) => {
        const i = side === "a" ? 0 : 1;
        const card = r.players[i];
        const p = card.player;
        const owner = ownerBadge(p.fantasy_team, p.availability_status);
        return (
          <div key={side} className={cx(styles.heroCard, sideClass(side))}>
            <span className={styles.sideTag}>Player {letter(side)}</span>
            <PlayerIdentity
              id={p.espn_player_id} name={p.full_name} position={p.position} team={p.pro_team} size="lg"
              meta={<>
                <Badge value={owner.text} tone={owner.tone} />
                {hasInjuryDesignation(card.availability.espn_status) && <Badge value={card.availability.espn_status} title="ESPN injury designation" />}
              </>}
            />
            <p className={styles.heroFacts}>
              {num(p.percent_owned, 1, "No ownership figure")}% owned ESPN-wide
              {p.match_confidence && !["high", "n/a"].includes(p.match_confidence) ? (
                <> · <Badge value={`${label(p.match_confidence)} usage match`} tone="warn" title="Matched to nflverse data by name rather than by an ID; verify before relying on the usage numbers" /></>
              ) : null}
              {p.match_method === "unmatched" ? <> · <Badge value="No usage data" tone="warn" title="No nflverse ID match for this ESPN player" /></> : null}
            </p>
          </div>
        );
      })}
    </div>
  );
}

/** Findings with their leaders, the limits of the comparison and the disclaimer. */
export function SummarySection({ r }: { r: CompareResponse }) {
  const na = playerName(r.players[0]);
  const nb = playerName(r.players[1]);
  return (
    <Section
      title="Research summary"
      subtitle="What the stored numbers show, one question at a time. A description of the data, not a verdict."
    >
      <ol className={styles.findings}>
        {r.summary.map((f) => (
          <li key={f.key} className={styles.finding}>
            <div className={styles.findingHead}>
              <h3 className={styles.findingTitle}>{f.title}</h3>
              <span className={cx(styles.leader, f.leader === "a" && styles.sideA, f.leader === "b" && styles.sideB)}>
                {f.leader === "a" || f.leader === "b" ? <span className={styles.sideDot} aria-hidden="true" /> : null}
                <span className={styles.leaderLabel}>{f.leader === "a" || f.leader === "b" ? "Leads: " : ""}</span>
                {leaderLabel(f.leader, na, nb)}
              </span>
            </div>
            <p className={styles.findingText}>{f.text}</p>
            <p className={styles.basis}>Basis: {f.basis}</p>
          </li>
        ))}
      </ol>
      {r.limitations.length > 0 && (
        <div className={styles.limits}>
          <h3 className={styles.limitsTitle}>Limits of this comparison</h3>
          <ul>{r.limitations.map((l, i) => <li key={i}>{l}</li>)}</ul>
        </div>
      )}
      <Notice tone="info" className={styles.disclaimer}>{r.disclaimer}</Notice>
    </Section>
  );
}

function GamesTable({ card, games }: { card: CompareCard; games: WeeklyEntry[] }) {
  const pos = card.player.position;
  const rushes = pos === "RB" || pos === "QB";
  const receives = pos === "RB" || pos === "WR" || pos === "TE";
  const usage = pos !== "K" && pos !== "D/ST" && pos !== "DST";
  return (
    <div className="table-wrap">
      <table className={styles.gamesTable}>
        <thead>
          <tr>
            <th scope="col">Wk</th><th scope="col">Opp</th>
            <th scope="col" className="num" title="ESPN points in this league's scoring">League pts</th>
            {usage && <th scope="col" className="num" title="nflverse full-PPR points">PPR</th>}
            {usage && <th scope="col" className="num" title="Expected PPR points from the workload (nflverse xFP)">xFP</th>}
            {usage && <th scope="col" className="num">Snap%</th>}
            {rushes && <th scope="col" className="num">Car</th>}
            {receives && <th scope="col" className="num">Tgt</th>}
          </tr>
        </thead>
        <tbody>
          {games.map((g) => (
            <tr key={g.week}>
              <th scope="row" className={styles.gameWeek}>{g.week}</th>
              <td>{g.opponent ?? <span className="missing" title="Opponent not recorded">—</span>}</td>
              <td className="num">{num(g.league_points, 1, "No ESPN score for this week")}</td>
              {usage && <td className="num">{num(g.ppr, 1, "No usage record")}</td>}
              {usage && <td className="num">{num(g.xfp, 1, "No usage record")}</td>}
              {usage && (
                <td className="num">
                  {g.snap_share === null || g.snap_share === undefined
                    ? <span className="missing" title="No snap record">—</span>
                    : `${Math.round(g.snap_share * 100)}%`}
                </td>
              )}
              {rushes && <td className="num">{g.carries ?? <span className="missing" title="No usage record">—</span>}</td>}
              {receives && <td className="num">{g.targets ?? <span className="missing" title="No usage record">—</span>}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PlayerContextColumn({ r, side }: { r: CompareResponse; side: Side }) {
  const card = r.players[side === "a" ? 0 : 1];
  const p = card.player;
  const av = availabilityView(card, r.as_of_week);
  const role = card.role;
  const dc = role.depth_chart;
  const currencyTone = av.currency === "stale" ? "warn" : av.currency === "current" ? "good" : "";
  return (
    <div className={cx(styles.contextCol, sideClass(side))}>
      <h3 className={styles.contextTitle}>
        <span className={styles.sideDot} aria-hidden="true" />
        <span className={styles.contextSide}>Player {letter(side)}</span>
        <Link className="player-link" href={`/players?id=${p.espn_player_id}`}>{playerName(card)}</Link>
      </h3>

      <dl className={styles.facts}>
        <dt>Designation</dt>
        <dd>
          <Badge value={av.label} tone={card.availability.has_designation ? "warn" : ""} />
          <span className={styles.factNote}>{av.note}</span>
        </dd>
        <dt>NFL report</dt>
        <dd>
          {card.availability.nfl_report
            ? <>{av.report ?? "No status listed"} <Badge value={av.currencyText} tone={currencyTone} /></>
            : <span className="faint">{av.currencyText}</span>}
        </dd>
        <dt>Depth chart</dt>
        <dd>
          {dc && (dc.pos_abb || dc.pos_rank !== null)
            ? <>{dc.pos_abb}{dc.pos_rank}{dc.team ? ` (${dc.team})` : ""}{dc.dt ? <span className="faint"> · as of {dc.dt}</span> : null}</>
            : <span className="missing" title="No depth chart entry stored">—</span>}
        </dd>
        <dt>Role</dt>
        <dd>
          {role.opportunity_tier ? <Badge value={role.opportunity_tier} title="Absolute opportunity tier" /> : <span className="missing" title="No role tier computed">—</span>}{" "}
          {role.role_trend ? <><TrendArrow label={role.role_trend} /> <span className="faint">{label(role.role_trend)} trend</span></> : null}
        </dd>
      </dl>

      <h4 className={styles.subTitle}>Role evidence</h4>
      {role.evidence.length > 0 ? (
        <ul className={styles.evidence}>
          {role.evidence.map((e, i) => (
            <li key={`${e.metric}-${i}`}>
              <TrendArrow label={e.label} /> {formatEvidence(e)}
              <span className="faint"> · wk {e.baseline_weeks.join(",") || "?"} to wk {e.recent_weeks.join(",") || "?"}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="faint">No metric has moved past its threshold between the baseline and recent games.</p>
      )}

      <h4 className={styles.subTitle}>Recent games <span className="faint">(newest first, through week {r.completed_through_week})</span></h4>
      {card.recent_games.length > 0
        ? <GamesTable card={card} games={card.recent_games} />
        : <p className="faint">No completed games with a score or usage record yet.</p>}
    </div>
  );
}

/** Per player: injury designation and report currency, role evidence and the last games. */
export function ContextSection({ r }: { r: CompareResponse }) {
  return (
    <Section
      title="Availability and context"
      subtitle={`Injury designations as of week ${r.as_of_week}. No designation is not confirmation a player will play.`}
      footnote={<>ESPN data synced {when(r.freshness.espn_synced_at)} · NFL data synced {when(r.freshness.nfl_synced_at)}
        {r.players.map((c, i) => c.projection?.projection_synced_at
          ? <span key={i}> · {playerName(c)} projections synced {when(c.projection.projection_synced_at)}</span> : null)}</>}
    >
      <div className={styles.contextGrid}>
        {SIDES.map((side) => <PlayerContextColumn key={side} r={r} side={side} />)}
      </div>
    </Section>
  );
}
