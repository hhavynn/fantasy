import Link from "next/link";
import { ShareBar } from "@/components/matchup/ShareBar";
import { Delta, Notice, Section } from "@/components/ui";
import type { ApiResult } from "@/lib/api";
import { cx } from "@/lib/cx";
import { num } from "@/lib/format";
import { providerName } from "@/lib/labels";
import { getLeagueProfile } from "@/lib/league-profile.server";
import { headline, margin, recordText, share, statusLabel, type Headline } from "@/lib/matchup";
import type { Matchup, TeamSide } from "@/lib/matchup-types";
import { decisionHref, type DecisionContext } from "@/lib/weekly-decisions";
import styles from "./home.module.css";

/** One side of the scoreboard: team, record, the headline total and what that total is. */
function Side({ side, h, status, mine, leads, projName, ctx }: {
  side: TeamSide; ctx: DecisionContext; h: Headline; status: Matchup["status"]; mine: boolean; leads: boolean;
  /** Whose projections a missing number lacks: "ESPN", or "Fantasy Intel" where the provider has none. */
  projName: string;
}) {
  return (
    <div className={cx(styles.side, mine ? styles.sideMe : styles.sideOpp)}>
      <p className={styles.sideLabel}>{mine ? "Selected team" : "Opponent"}</p>
      <p className={styles.teamName}>
        <Link href={decisionHref(`/managers/${side.team_id}`, ctx)}>{side.name}</Link>
      </p>
      <p className={styles.teamRecord}>{recordText(side.record)}</p>
      <p className={cx(styles.teamTotal, leads && styles.leads)}>
        {num(h.value, 1, status === "final" ? "No score recorded" : "No projection for this week")}
      </p>
      <p className={styles.teamNote}>
        {h.label}{h.incomplete ? `: some players have no ${projName} projection, so this is not a full-lineup projection` : ""}
      </p>
    </div>
  );
}

/** Compact matchup scoreboard: both teams, totals, margin and share. Degrades to a notice when the call failed. Links
 *  keep the hub's league, team and week. */
export async function MatchupCard({ result, ctx }: { result: ApiResult<Matchup>; ctx: DecisionContext }) {
  const profile = await getLeagueProfile();
  const projName = profile.providerProjections ? "ESPN" : "Fantasy Intel";
  const link = <Link className={styles.cardLink} href={decisionHref("/matchup", ctx)}>Full matchup →</Link>;
  if (!result.ok) {
    return (
      <Section title="This week" actions={link}>
        <Notice tone="caution" title="Matchup unavailable">{result.error}</Notice>
      </Section>
    );
  }
  const m = result.data;
  const when = `Week ${m.week} · ${statusLabel(m.status)}`;
  if (!m.opponent) {
    const mine = headline(m.me, m.status);
    return (
      <Section title="This week" subtitle={when} actions={link}>
        <div className={styles.card}>
          <p className={styles.cardNone}>
            {m.is_bye_week ? `${m.me.name} has a bye this week.` : `No opponent is scheduled in the synced ${providerName(profile.provider)} schedule.`}
          </p>
          {mine.value !== null ? <div className={`${styles.board} ${styles.boardSolo}`}><Side side={m.me} h={mine} status={m.status} mine leads={false} projName={projName} ctx={ctx} /></div> : null}
        </div>
      </Section>
    );
  }
  const me = headline(m.me, m.status);
  const opp = headline(m.opponent, m.status);
  const mg = margin(me, opp);
  const pct = share(me, opp);
  const label = mg.basis === "projected" ? "Projected margin" : m.status === "final" ? "Final margin" : "Margin so far";
  const meLeads = mg.value !== null && mg.value > 0;
  const oppLeads = mg.value !== null && mg.value < 0;
  return (
    <Section title="This week" subtitle={when} actions={link}>
      <div className={styles.card}>
        <div className={styles.board}>
          <Side side={m.me} h={me} status={m.status} mine leads={meLeads} projName={projName} ctx={ctx} />
          <span className={styles.vs} aria-hidden="true">vs</span>
          <Side side={m.opponent} h={opp} status={m.status} mine={false} leads={oppLeads} projName={projName} ctx={ctx} />
        </div>
        {mg.value !== null ? (
          <p className={styles.cardMargin}>
            <span className={styles.cardMarginText}>{label}</span>
            <span className={styles.cardMarginValue}><Delta value={mg.value} /></span>
            <span className={styles.cardMarginText}>{mg.value === 0 ? "even" : `${mg.value > 0 ? m.me.name : m.opponent.name} ahead`}</span>
          </p>
        ) : (
          <p className={styles.cardNone}>{mg.reason}</p>
        )}
        {pct !== null && mg.basis ? <ShareBar share={pct} meName={m.me.name} oppName={m.opponent.name} basis={mg.basis} /> : null}
      </div>
    </Section>
  );
}
