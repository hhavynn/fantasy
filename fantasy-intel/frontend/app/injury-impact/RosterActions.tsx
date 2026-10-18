import Link from "next/link";
import type { ReactNode } from "react";
import { Badge } from "@/components/ui";
import { gameLabel, levelBadge, levelLabel, rosterActions, waiversHref, type InjuryEntry, type RosterAction } from "@/lib/injury-impact";
import { withTeam } from "@/lib/team-perspective";
import { Change, RosterBadge, Who } from "./ImpactParts";
import styles from "./injury-impact.module.css";

const UNIT = { touches: "touches", targets: "targets", pass_attempts: "att" } as const;

/** "if Breece Hall sits: estimate +18.9 touches/g · observed +14.0 over 2 games". */
function GainLine({ a }: { a: RosterAction }) {
  const m = a.mate;
  if (!m) return null;
  const unit = UNIT[a.entry.impact.unit];
  const without = m.observed.without?.games ?? 0;
  return (
    <span className={styles.actionWhy}>
      {a.entry.full_name} ({levelLabel(a.entry.status.level).toLowerCase()}) ·{" "}
      {m.estimate?.extra_per_game !== null && m.estimate?.extra_per_game !== undefined
        ? <>estimate +{m.estimate.extra_per_game.toFixed(1)} {unit}/g</> : "no estimate"}
      {m.observed.delta_per_game !== null
        ? <> · observed <Change v={m.observed.delta_per_game} /> over {without} {without === 1 ? "game" : "games"}</>
        : <> · no observed games without him</>}
    </span>
  );
}

function ActionList({ title, empty, items, render }: {
  title: string; empty: string; items: RosterAction[]; render: (a: RosterAction) => ReactNode;
}) {
  return (
    <div className={styles.actionCol}>
      <h3 className={styles.blockTitle}>{title} <span className={styles.count}>{items.length}</span></h3>
      {items.length === 0 ? <p className={styles.none}>{empty}</p> : (
        <ul className={styles.actionList}>{items.slice(0, 6).map((a) => (
          <li key={`${a.entry.gsis_id}-${a.mate?.gsis_id ?? "self"}`}>{render(a)}</li>
        ))}</ul>
      )}
      {items.length > 6 && <p className={styles.none}>{items.length - 6} more in the list below.</p>}
    </div>
  );
}

/** Decision first: the selected roster's injured players, its players who could gain, and pickups. */
export function RosterActions({ entries, team }: { entries: InjuryEntry[]; team: string | null }) {
  const { myInjured, myGainers, pickups } = rosterActions(entries);
  return (
    <div className={styles.actions}>
      <ActionList title="Your injured players" items={myInjured}
        empty="None of your players carries an injury designation for a game still to play."
        render={(a) => (
          <>
            <Who id={a.entry.espn_player_id} name={a.entry.full_name} position={a.entry.position} team={a.entry.team}
              meta={<Badge value={levelBadge(a.entry.status.level)} />} />
            <span className={styles.actionWhy}>
              {gameLabel(a.entry.game) ?? "Opponent unknown"} · <Link href={withTeam("/start-sit", team)}>Check Start/Sit</Link>
            </span>
          </>
        )} />
      <ActionList title="Your players who could gain" items={myGainers}
        empty="No player on your roster is a likely beneficiary of a current absence."
        render={(a) => (
          <>
            <Who id={a.mate!.espn_player_id} name={a.mate!.full_name} position={a.mate!.position} team={a.entry.team} />
            <GainLine a={a} />
          </>
        )} />
      <ActionList title="Available teammates" items={pickups}
        empty="No free agent or waiver player stands out as absorbing an absent player's volume."
        render={(a) => (
          <>
            <Who id={a.mate!.espn_player_id} name={a.mate!.full_name} position={a.mate!.position} team={a.entry.team}
              meta={<RosterBadge roster={a.mate!.roster} />} />
            <GainLine a={a} />
            <span className={styles.actionWhy}><Link href={withTeam(waiversHref(a.mate!.position), team)}>See {a.mate!.position} waivers</Link></span>
          </>
        )} />
    </div>
  );
}
