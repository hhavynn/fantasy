import Link from "@/components/LeagueLink";
import { BackendProblem } from "@/components/BackendProblem";
import { Notice, PageHeader, Section } from "@/components/ui";
import { api } from "@/lib/api";
import { when } from "@/lib/format";
import { POSITION_NAME, POSITION_OF, SLUGS, apiQuery, noSeasonData, parseResearchState, poolSentence } from "@/lib/research";
import type { OpportunityResponse } from "@/lib/research-types";
import { ResearchGroups } from "./Tables";
import styles from "./research.module.css";

export const metadata = { title: "Research lab" };

const DEFAULT_STATE = parseResearchState({});

/** Lab overview: what the lab answers, then each position's research lists at the default filters. */
export default async function ResearchOverview() {
  const results = await Promise.all(SLUGS.map((s) => api<OpportunityResponse>(`/api/research/opportunity?${apiQuery(POSITION_OF[s], DEFAULT_STATE)}`)));
  const failed = results.find((r) => !r.ok);
  const first = results.find((r) => r.ok && r.data.data)?.data ?? null;
  const d = first?.data ?? null;
  return (
    <div className="stack">
      <PageHeader
        eyebrow="Players · Research"
        title="Research lab"
        meta={d ? <>{first?.season} season · nflverse data through week {d.latest_week ?? "—"} · full PPR · NFL sync {when(d.nfl_synced_at)}</> : "Who is getting the opportunities, and is production keeping up?"}
      >
        <nav className={styles.posLinks} aria-label="Positions">
          {SLUGS.map((s) => <Link key={s} href={`/research/${s}`} className={`${styles.posLink} pos-${POSITION_OF[s]}`}>{POSITION_NAME[POSITION_OF[s]]} →</Link>)}
          <Link href="/research/splits" className={styles.posLink}>Game splits →</Link>
        </nav>
      </PageHeader>

      <p className={styles.lede}>
        Every WR, RB and TE in the NFL, not only your league&apos;s rosters. The lab compares the fantasy points a player&apos;s role is
        expected to produce (xFP) with what the player actually scored, shows the workload behind it, and flags roles that are growing before the box
        score catches up. The lists below are where to start looking; each position page has the charts, a sortable leaderboard and the evidence for any player.
      </p>

      {failed && !failed.ok && <BackendProblem error={failed.error} kind={failed.kind} status={failed.status} />}

      {results.map((res, i) => {
        const slug = SLUGS[i];
        const pos = POSITION_OF[slug];
        if (!res.ok) return null;
        const r = res.data;
        return (
          <Section key={slug} title={POSITION_NAME[pos]} actions={<Link href={`/research/${slug}`}>Open {pos} research</Link>}
            footnote={r.status === "ok" ? `${poolSentence(r)}. Exploratory lists, not recommendations.` : undefined}>
            {r.status === "no_data" ? <Notice tone="info">{r.message}</Notice>
              : noSeasonData(r) ? <Notice tone="info">No NFL usage data is available for the {r.season} season yet.</Notice>
              : r.status === "empty" ? <p className={styles.noteTight}>No {pos} meets the default filters yet.</p>
                : <ResearchGroups slug={slug} r={r} state={DEFAULT_STATE} limit={3} />}
          </Section>
        );
      })}

      <Section title="How to read the lab">
        <dl className={styles.glossary}>
          <div><dt>xFP/G</dt><dd>Expected fantasy points per game: what an average player would score in full PPR from the same targets and carries. It measures the role.</dd></div>
          <div><dt>FPOE/G</dt><dd>Fantasy points over expectation: actual PPR minus xFP, per game, over the same games. Negative means the box score trails the role.</dd></div>
          <div><dt>WOPR</dt><dd>Weighted opportunity rating, 1.5 × target share + 0.7 × air-yard share: a receiver&apos;s share of the passing game, weighting targets over depth.</dd></div>
          <div><dt>Backfield share</dt><dd>A running back&apos;s designed runs over all running-back designed runs. Unlike team rush share it ignores quarterback runs and scrambles.</dd></div>
          <div><dt>Momentum</dt><dd>Recent games against earlier games for expected and actual points. A rising xFP with flat points is a role growing ahead of production.</dd></div>
        </dl>
        <p className={styles.noteTight}>A negative FPOE is not proof of a buy-low and a positive one is not proof of a sell: elite players beat an average-player model for long stretches. Treat every list as a question to investigate.</p>
      </Section>
    </div>
  );
}
