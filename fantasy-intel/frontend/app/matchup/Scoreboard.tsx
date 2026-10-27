import Link from "@/components/LeagueLink";
import { ShareBar } from "@/components/matchup/ShareBar";
import { Delta, Section } from "@/components/ui";
import { num } from "@/lib/format";
import { headline, margin, ordinal, recordText, share, type Headline } from "@/lib/matchup";
import type { Matchup, TeamSide } from "@/lib/matchup-types";
import styles from "./matchup.module.css";

function Team({ side, h, status, mine, projBy }: { side: TeamSide; h: Headline; status: Matchup["status"]; mine: boolean; projBy: string }) {
  const projected = h.basis === "actual" && side.projected_points !== null ? side.projected_points : null;
  return (
    <div className={`${styles.team} ${mine ? "" : styles.opp}`}>
      <h2 className={styles.teamName}><Link href={`/managers/${side.team_id}`}>{side.name}</Link></h2>
      <p className={styles.teamMeta} title="Season record to date, not as of the week shown">
        {recordText(side.record)}{side.standing ? ` · ${ordinal(side.standing)} in standings` : ""}
      </p>
      <p className={styles.total}>{num(h.value, 1, status === "final" ? "No score recorded" : "No projection for this week")}</p>
      <p className={styles.totalLabel}>{h.label}</p>
      {projected !== null ? <p className={styles.totalAside}>Projected {projected.toFixed(1)}</p> : null}
      {h.incomplete ? (
        <p className={styles.incomplete}>Incomplete: some rostered players have no {projBy} projection, so this is not a full-lineup projection.</p>
      ) : null}
    </div>
  );
}

/** You against the opponent: totals in the display face, margin, and my share of the combined points. */
/** `projBy`: whose projections a missing number lacks ("ESPN", or "Fantasy Intel" where the provider has none). */
export function Scoreboard({ m, opponent, projBy = "ESPN" }: { m: Matchup; opponent: TeamSide; projBy?: string }) {
  const me = headline(m.me, m.status);
  const opp = headline(opponent, m.status);
  const mg = margin(me, opp);
  const pct = share(me, opp);
  const marginLabel = mg.basis === "projected" ? "Projected margin" : m.status === "final" ? "Final margin" : "Margin so far";
  return (
    <Section>
      <div className={styles.board} role="group" aria-label="Scoreboard">
        <Team side={m.me} h={me} status={m.status} mine projBy={projBy} />
        <span className={styles.versus} aria-hidden="true">vs</span>
        <Team side={opponent} h={opp} status={m.status} mine={false} projBy={projBy} />
        <div className={styles.summary}>
          {mg.value !== null ? (
            <p className={styles.margin}>
              <span className={styles.marginText}>{marginLabel}</span>
              <span className={styles.marginValue}><Delta value={mg.value} /></span>
              <span className={styles.marginText}>
                {mg.value === 0 ? "even" : `${mg.value > 0 ? m.me.name : opponent.name} ahead`}
              </span>
            </p>
          ) : (
            <p className={styles.marginNone}>{mg.reason}</p>
          )}
          {pct !== null && mg.basis ? <ShareBar share={pct} meName={m.me.name} oppName={opponent.name} basis={mg.basis} /> : null}
        </div>
      </div>
    </Section>
  );
}
