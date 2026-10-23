import Link from "@/components/LeagueLink";
import type { ReactNode } from "react";
import { BackendProblem } from "@/components/BackendProblem";
import { Badge, EmptyState, Glossary, Method, Notice, PageHeader, Section, Stat, StatStrip } from "@/components/ui";
import { api } from "@/lib/api";
import { Missing, when } from "@/lib/format";
import {
  eligibleText, groupRules, isLeagueRules, pointsText, ruleDetail, rosterSummary, rulesHeadline, statusCounts,
  statusLabel, unmappedValue, type EngineStatus, type Fact, type LeagueRules,
} from "@/lib/league-rules";
import { plural } from "@/lib/plural";
import styles from "./rules.module.css";

export const metadata = { title: "League rules" };

const QUESTION = "How this league scores, which players each lineup slot takes, and how waivers and trades work.";

// Read-only league rules: what the provider reported at the last sync, with the engines' support for each rule. No
// forms and no edit controls: a manager changes these settings on the provider's own site.

export default async function LeagueRulesPage() {
  const res = await api<LeagueRules>("/api/league-rules");
  if (!res.ok || !isLeagueRules(res.data)) {
    const error = res.ok ? "The backend answered in a format this page does not read." : res.error;
    const notSynced = !res.ok && res.status === 503 && res.kind === "http" && !/no database yet/i.test(res.error);
    return (
      <div className="stack">
        <PageHeader eyebrow="League" title="League rules" meta={QUESTION} />
        {notSynced ? (
          <Section>
            <EmptyState title="Your league isn't synced yet" action={<Link href="/data">Check data status</Link>}>
              <p>The rules come from the league settings saved by a sync. Run the sync, then reload this page.</p>
              <p className="faint">{error}</p>
            </EmptyState>
          </Section>
        ) : <BackendProblem error={error} kind={res.ok ? "http" : res.kind} status={res.ok ? undefined : res.status} />}
      </div>
    );
  }
  const d = res.data;
  const provider = d.source.provider_label;
  return (
    <div className="stack">
      <PageHeader eyebrow={d.league.season ? `League · ${d.league.season} season` : "League"} title="League rules" meta={QUESTION} />
      <Notice title={`Source: ${provider} league settings, read only`}>
        <p>
          {d.league.name ? <>{d.league.name}, as </> : "As "}captured by the last sync
          {d.source.captured_at ? <> ({when(d.source.captured_at)})</> : null}. Nothing here is fetched live and nothing
          here can change the league: settings are edited on {provider}.
        </p>
      </Notice>
      <StatStrip>
        <Stat label="Scoring" value={d.scoring.format.display ?? "—"} note={d.scoring.format.reason ?? (rulesHeadline(d).split(" · ").slice(1).join(" · ") || undefined)} />
        <Stat label="Starters" value={d.roster.starters.display ?? "—"} note={rosterSummary(d.roster) || d.roster.starters.reason || undefined} />
        <Stat label="Waivers" value={d.waivers.type.value === "faab" ? `FAAB ${d.waivers.faab_budget.display ?? ""}`.trim() : d.waivers.type.value ? "Waiver order" : "—"}
          note={d.waivers.type.display ?? d.waivers.type.reason ?? undefined} />
        <Stat label="Trade deadline" value={d.trades.deadline.display ?? "—"} note={d.trades.deadline.reason ?? undefined} />
      </StatStrip>

      <EngineSupport d={d} />
      <Slots d={d} />
      <Scoring d={d} />
      <Transactions d={d} />
      <Unmapped d={d} />

      <Glossary terms={["PPR", "FLEX", "IR", "FAAB", "Kickoff lock", "—"]} />
      <Method title="Where these rules come from">
        <p>{d.source.note}</p>
        <p>
          Every value is read from the {provider} settings the last sync saved. A dash means the snapshot does not hold
          that setting (hover it for the reason); it never stands for zero. Provider settings this page cannot read are
          listed under &ldquo;Provider rules not interpreted&rdquo; rather than dropped.
        </p>
        <p>
          Engine support says whether the lineup, projection, waiver and trade analysis applies each rule this league
          uses. &ldquo;Not applied&rdquo; means recommendations ignore that rule, so check them against it yourself.
        </p>
      </Method>
    </div>
  );
}

function Value({ f }: { f: Fact<unknown> }) {
  return f.display !== null ? <>{f.display}</> : <Missing why={f.reason ?? "Not in the synced settings"} />;
}

function StatusBadge({ status }: { status: EngineStatus | string }) {
  const s = statusLabel(status);
  return <Badge value={s.label} tone={s.tone} />;
}

function EngineSupport({ d }: { d: LeagueRules }) {
  const counts = statusCounts(d.engine_support);
  return (
    <Section
      title="Engine support"
      subtitle={counts.map((c) => `${c.count} ${statusLabel(c.status).label.toLowerCase()}`).join(" · ") || "No rules to check"}
      flush
      footnote="Rules the engines do not apply are not reflected in lineup, waiver or trade recommendations."
    >
      <div className="table-wrap">
        <table className={styles.table}>
          <thead><tr><th>Rule</th><th>Status</th><th>What the analysis does</th></tr></thead>
          <tbody>
            {d.engine_support.map((r) => (
              <tr key={r.rule}>
                <td>{r.rule}</td>
                <td><StatusBadge status={r.status} /></td>
                <td className={styles.wrap}>{r.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

function Slots({ d }: { d: LeagueRules }) {
  const r = d.roster;
  const extra: string[] = [];
  if (r.superflex) extra.push("The OP (superflex) slot takes a quarterback as well as RB, WR and TE.");
  if (r.flex_types.length > 1) extra.push(`Flex slots: ${r.flex_types.join(", ")}.`);
  return (
    <Section id="roster" title="Lineup slots" subtitle={rosterSummary(r) || undefined} flush
      footnote={<>Per-position roster limits: <Value f={r.position_limits} />{extra.length ? <> · {extra.join(" ")}</> : null}</>}>
      {r.slots.length === 0 ? (
        <p className={styles.pad}><Missing why={r.starters.reason ?? "No slots synced"} /> {r.starters.reason}</p>
      ) : (
        <div className="table-wrap">
          <table className={styles.table}>
            <thead><tr><th>Slot</th><th className="num">Count</th><th>Who can play it</th><th>Engine support</th></tr></thead>
            <tbody>
              {r.slots.map((s) => {
                const who = eligibleText(s);
                return (
                  <tr key={s.code}>
                    <td><b>{s.code}</b> <span className="faint">{s.name !== s.code ? s.name : null}</span></td>
                    <td className="num">{s.count}</td>
                    <td>{who ?? <Missing why={s.eligible_reason ?? "Not mapped"} />}</td>
                    <td className={styles.wrap}><StatusBadge status={s.engine.status} /> <span className="faint">{s.engine.note}</span></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}

function Scoring({ d }: { d: LeagueRules }) {
  const s = d.scoring;
  const groups = groupRules(s.rules);
  return (
    <>
      <Section id="scoring" title="Scoring" subtitle={s.format.display ?? undefined} flush>
        <div className="table-wrap">
          <table className={styles.table}>
            <thead><tr><th>Rule</th><th>League value</th></tr></thead>
            <tbody>
              <Row label="Format" f={s.format} />
              <Row label="Points per reception" f={s.points_per_reception} />
              <Row label="TE premium" f={s.te_premium} />
              <Row label="Scoring type" f={s.scoring_type} />
              {s.highlights.map((h) => <Row key={h.label} label={h.label} f={h} />)}
            </tbody>
          </table>
        </div>
      </Section>
      <Section title="All scoring rules" subtitle={`${s.rules.length} ${plural(s.rules.length, "rule")} from ${s.rules_source}`} flush
        footnote={s.zero_rules ? `${s.zero_rules} ${plural(s.zero_rules, "rule")} worth 0 points ${s.zero_rules === 1 ? "is" : "are"} not listed.` : undefined}>
        {groups.length === 0 ? (
          <p className={styles.pad}><Missing why={s.format.reason ?? "No rules synced"} /> {s.format.reason ?? "No scoring rules were synced."}</p>
        ) : (
          <div className="table-wrap">
            <table className={styles.table}>
              <thead><tr><th>Statistic</th><th className="num">Points</th><th>Applies to</th></tr></thead>
              {groups.map((g) => (
                <tbody key={g.group}>
                  <tr className={styles.group}><th colSpan={3} scope="colgroup">{g.group}</th></tr>
                  {g.rules.map((r) => (
                    <tr key={r.key}>
                      <td>{r.label}{r.mapped ? null : <span className="faint"> (provider key {r.key})</span>}</td>
                      <td className="num">{pointsText(r.points)}</td>
                      <td>{ruleDetail(r) ?? <span className="faint">Every eligible player</span>}</td>
                    </tr>
                  ))}
                </tbody>
              ))}
            </table>
          </div>
        )}
      </Section>
    </>
  );
}

function Row({ label, f }: { label: ReactNode; f: Fact<unknown> }) {
  return <tr><td>{label}</td><td><Value f={f} /></td></tr>;
}

function Transactions({ d }: { d: LeagueRules }) {
  return (
    <Section id="transactions" title="Lineup lock, waivers, trades and playoffs" flush>
      <div className="table-wrap">
        <table className={styles.table}>
          <thead><tr><th>Setting</th><th>League value</th></tr></thead>
          <tbody>
            <Row label="Lineup lock" f={d.lineup_lock} />
            <Row label="Waiver system" f={d.waivers.type} />
            <Row label="FAAB budget" f={d.waivers.faab_budget} />
            <Row label="Waiver period" f={d.waivers.period} />
            <Row label="Claims process" f={d.waivers.process_days} />
            <Row label="Trade deadline" f={d.trades.deadline} />
            <Row label="Trade review" f={d.trades.review} />
            <Row label="Teams" f={d.league.team_count} />
            <Row label="League type" f={d.league.league_type} />
            <Row label="Regular season" f={d.playoffs.regular_season} />
            <Row label="Playoffs start" f={d.playoffs.start} />
            <Row label="Playoff teams" f={d.playoffs.teams} />
            <Row label="Playoff rounds" f={d.playoffs.round_length} />
            <Row label="Final week" f={d.playoffs.final_week} />
          </tbody>
        </table>
      </div>
    </Section>
  );
}

function Unmapped({ d }: { d: LeagueRules }) {
  if (d.unmapped.length === 0) return null;
  const n = d.unmapped.length;
  return (
    <Section id="unmapped" title="Provider rules not interpreted"
      subtitle={`${n} ${plural(n, "setting")} from ${d.source.provider_label} this page lists verbatim; the analysis does not use them`} flush>
      <details className={styles.details}>
        <summary>Show {n} {plural(n, "setting")}</summary>
        <div className="table-wrap">
          <table className={styles.table}>
            <thead><tr><th>Provider key</th><th>Area</th><th>Value</th><th>Why it is listed</th></tr></thead>
            <tbody>
              {d.unmapped.map((u, i) => {
                const v = unmappedValue(u.value);
                return (
                  <tr key={`${u.area}-${u.key}-${i}`}>
                    <td className={styles.code}>{u.key}</td>
                    <td>{u.area}</td>
                    <td>{v ?? <Missing why="Structured value not shown" />}</td>
                    <td className={styles.wrap}>{u.reason}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </details>
    </Section>
  );
}
