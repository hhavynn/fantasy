import Link from "@/components/LeagueLink";
import { PlayerIdentity } from "@/components/PlayerIdentity";
import { PlayerLink } from "@/components/PlayerLink";
import { Delta, Section, Stat, StatStrip, VisuallyHidden } from "@/components/ui";
import { Missing } from "@/lib/format";
import { cx } from "@/lib/cx";
import { plural } from "@/lib/plural";
import {
  PCT_MISSING, beforeAfterText, biggestMovers, moversHref, pctText, rankText, rankTitle, signedPts, totalsText,
  type Mover, type MoversResponse, type MoversState,
} from "@/lib/projection-movers";
import styles from "./movers.module.css";

/** The answer in one strip: who rose most, who fell most, and how many players were compared. */
export function Answer({ d }: { d: MoversResponse }) {
  const { riser, faller } = biggestMovers(d);
  const min = d.thresholds.min_delta;
  const s = d.summary;
  return (
    <StatStrip>
      <Stat label="Biggest riser" value={riser ? signedPts(riser.delta) : "—"} tone={riser ? "positive" : undefined}
        note={riser ? <MoverNote m={riser} /> : `No relevant player rose by ${min} points or more`} />
      <Stat label="Biggest faller" value={faller ? signedPts(faller.delta) : "—"} tone={faller ? "negative" : undefined}
        note={faller ? <MoverNote m={faller} /> : `No relevant player fell by ${min} points or more`} />
      <Stat label="Players compared" value={s.compared}
        note={`${s.risers} up · ${s.fallers} down · ${s.unchanged} flat`} />
    </StatStrip>
  );
}

function MoverNote({ m }: { m: Mover }) {
  return <><PlayerLink id={m.espn_player_id} name={m.name} /> · {m.position}{m.nfl_team ? ` · ${m.nfl_team}` : ""} · {beforeAfterText(m)}</>;
}

/** "Details" jumps to the selected player's chart and evidence further down the page. */
function DetailLink({ state, m }: { state: MoversState; m: Mover }) {
  return (
    <Link className={styles.detailLink} href={`${moversHref(state, { player: m.espn_player_id })}#detail`} prefetch={false}>
      Details<VisuallyHidden> for {m.name}</VisuallyHidden>
    </Link>
  );
}

function Row({ m, index, state, selected }: { m: Mover; index: number; state: MoversState; selected: boolean }) {
  const pct = pctText(m.pct);
  const rank = rankText(m);
  return (
    <li className={cx(styles.row, selected && styles.selected)} aria-current={selected ? "true" : undefined}>
      <span className={styles.no} aria-hidden="true">{index + 1}</span>
      <PlayerIdentity id={m.espn_player_id} name={m.name} position={m.position} team={m.nfl_team} className={styles.who} />
      <span className={styles.change}>
        <span className={styles.big}><Delta value={m.delta} /></span>
        <span className={styles.pct}>{pct ?? <Missing why={PCT_MISSING} />}</span>
      </span>
      <span className={styles.facts}>
        <span title="Projected points, earlier forecast → latest forecast">{beforeAfterText(m)} pts</span>
        <span title={rankTitle(m)}>{rank ?? <Missing why={rankTitle(m)} />}</span>
      </span>
      <span className={styles.evid}>
        <span className={styles.evText}>
          {m.evidence?.headline ?? (
            <>
              <Missing why={m.evidence ? "No recorded change in usage, availability or role passed the thresholds" : "Evidence is not attached to this player"} />
              <VisuallyHidden>No recorded change found</VisuallyHidden>
            </>
          )}
        </span>
        <DetailLink state={state} m={m} />
      </span>
    </li>
  );
}

export function MoverList({ rows, state, selectedId, empty }: {
  rows: Mover[]; state: MoversState; selectedId: number | null; empty: string;
}) {
  if (rows.length === 0) return <p className={styles.emptyList}>{empty}</p>;
  return (
    <ol className={styles.rows}>
      {rows.map((m, i) => <Row key={m.espn_player_id} m={m} index={i} state={state} selected={m.espn_player_id === selectedId} />)}
    </ol>
  );
}

/** Two ranked lists: side by side on desktop, stacked on phones. */
export function RisersFallers({ d, state, selectedId }: { d: MoversResponse; state: MoversState; selectedId: number | null }) {
  const sub = `${totalsText(d.window.weeks)}, earlier → latest forecast`;
  const min = d.thresholds.min_delta;
  const per = d.thresholds.relevant_per_week;
  return (
    <>
      <div className={styles.pair}>
        <Section title="Biggest risers" subtitle={sub} flush>
          <MoverList rows={d.risers} state={state} selectedId={selectedId}
            empty={`No player averaging ${per} projected points a week or more rose by ${min} ${plural(min, "point")} or more.`} />
        </Section>
        <Section title="Biggest fallers" subtitle={sub} flush>
          <MoverList rows={d.fallers} state={state} selectedId={selectedId}
            empty={`No player averaging ${per} projected points a week or more fell by ${min} ${plural(min, "point")} or more.`} />
        </Section>
      </div>
      <p className={styles.listNote}>
        Lists show players averaging at least {per} projected points a week in either forecast and moving by at least {min} points;
        every compared player is in the table below. Recorded data is shown alongside the change and is not shown to be its cause.
      </p>
    </>
  );
}
