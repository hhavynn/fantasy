import Link from "next/link";
import { ShareBar } from "@/components/matchup/ShareBar";
import { Delta, Notice, Section } from "@/components/ui";
import type { ApiResult } from "@/lib/api";
import { num } from "@/lib/format";
import { headline, margin, recordText, share, statusLabel, type Headline } from "@/lib/matchup";
import type { Matchup, TeamSide } from "@/lib/matchup-types";
import styles from "./home.module.css";

function Row({ side, h, status }: { side: TeamSide; h: Headline; status: Matchup["status"] }) {
  return (
    <div className={styles.teamRow}>
      <p className={styles.teamName}>
        <Link href={`/managers/${side.team_id}`}>{side.name}</Link> <span className={styles.teamRecord}>{recordText(side.record)}</span>
      </p>
      <p className={styles.teamTotal}>{num(h.value, 1, status === "final" ? "No score recorded" : "No projection for this week")}</p>
      <p className={styles.teamNote}>
        {h.label}{h.incomplete ? ": some players have no ESPN projection, so this is not a full-lineup projection" : ""}
      </p>
    </div>
  );
}

/** Compact matchup: both teams, totals, margin and share. Degrades to a notice when the call failed. */
export function MatchupCard({ result }: { result: ApiResult<Matchup> }) {
  const link = <Link className={styles.cardLink} href="/matchup">Full matchup →</Link>;
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
      <Section title="This week" actions={link}>
        <div className={styles.card}>
          <p className={styles.cardWhen}>{when}</p>
          <p className={styles.cardNone}>
            {m.is_bye_week ? `${m.me.name} has a bye this week.` : "No opponent is scheduled in the synced ESPN schedule."}
          </p>
          {mine.value !== null ? <Row side={m.me} h={mine} status={m.status} /> : null}
        </div>
      </Section>
    );
  }
  const me = headline(m.me, m.status);
  const opp = headline(m.opponent, m.status);
  const mg = margin(me, opp);
  const pct = share(me, opp);
  const label = mg.basis === "projected" ? "Projected margin" : m.status === "final" ? "Final margin" : "Margin so far";
  return (
    <Section title="This week" actions={link}>
      <div className={styles.card}>
        <p className={styles.cardWhen}>{when}</p>
        <div className={styles.teams}>
          <Row side={m.me} h={me} status={m.status} />
          <Row side={m.opponent} h={opp} status={m.status} />
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
