import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { cache } from "react";
import { BackendProblem } from "@/components/BackendProblem";
import { LineChart } from "@/components/charts";
import { MovesTable } from "@/components/MovesTable";
import { PlayerLink } from "@/components/PlayerLink";
import { Badge, EmptyState, Method, Notice, PageHeader, Pos, Rating, Score, Section, Stat, StatStrip, Tabs } from "@/components/ui";
import { api, type ApiResult } from "@/lib/api";
import { date, num } from "@/lib/format";
import { RATING_CAVEAT, RATING_DESCRIPTION, ratingFor } from "@/lib/ratings";
import { compositionSeries, parseTab } from "@/lib/manager-view";
import { ConfDot, DIMENSION_TITLES, formatMetric, labelTone } from "@/lib/managers";
import type { ScoutingReport as Report } from "@/lib/manager-scouting";
import type { Classification, League, Metric, Move, Need, RosterPlayer, Team } from "@/lib/types";
import { ScoutingEvidence, ScoutingSummary } from "./ScoutingReport";
import styles from "./detail.module.css";


type Detail = {
  league: League; team: Team; metrics: Metric[]; classifications: Classification[]; roster: RosterPlayer[];
  weekly_composition: { scoring_period: number; position: string; n: number; starters: number }[];
  needs: Need[]; moves: Move[];
  failed_claims: { transaction_id: string; status: string; scoring_period: number; bid_amount: number | null; date: string | null; full_name: string | null; position: string | null }[];
};

const POS = ["QB", "RB", "WR", "TE", "K", "D/ST"];

// One fetch per request, shared by the page and its title.
const getDetail = cache((teamId: string) => api<Detail>(`/api/managers/${teamId}`));

export async function generateMetadata({ params }: PageProps<"/managers/[teamId]">): Promise<Metadata> {
  const { teamId } = await params;
  const res = await getDetail(teamId);
  return { title: res.ok ? res.data.team.name : "Manager" };
}

export default async function ManagerDetail({ params, searchParams }: PageProps<"/managers/[teamId]">) {
  const [{ teamId }, sp] = await Promise.all([params, searchParams]);
  const tab = parseTab(sp.tab);
  const [res, scouting] = await Promise.all([
    getDetail(teamId),
    // Transactions need no scouting data.
    tab === "transactions" ? Promise.resolve(null) : api<Report>(`/api/managers/${teamId}/scouting`),
  ]);
  if (!res.ok && res.status === 404) notFound();
  if (!res.ok) {
    return (
      <div className="stack">
        <PageHeader title="Manager" actions={<Link href="/managers">← All managers</Link>} />
        <BackendProblem error={res.error} kind={res.kind} status={res.status} />
      </div>
    );
  }
  const d = res.data;
  const t = d.team;
  const record = `${t.wins}–${t.losses}${t.ties ? `–${t.ties}` : ""}`;
  const items = [
    { href: "?tab=overview", label: "Overview" },
    { href: "?tab=scouting", label: "Scouting" },
    { href: "?tab=transactions", label: "Transactions", count: d.moves.length },
  ];

  return (
    <div className="stack">
      <PageHeader
        eyebrow="Manager scouting"
        title={<>{t.name}{t.is_mine && <> <Badge value="You" tone="info" /></>}</>}
        meta={`${t.owner_names ?? "Owner unknown"} · ${record} · PF ${num(t.points_for, 1)}${t.playoff_seed ? ` · seed ${t.playoff_seed}` : ""}`}
        actions={<Link href="/managers">← All managers</Link>}
      >
        <Tabs label="Manager views" items={items} current={`?tab=${tab}`} />
      </PageHeader>

      {tab === "overview" && <Overview d={d} scouting={scouting} />}
      {tab === "scouting" && (scouting?.ok
        ? <ScoutingEvidence report={scouting.data} />
        : scouting ? <BackendProblem error={scouting.error} kind={scouting.kind} status={scouting.status} /> : null)}
      {tab === "transactions" && <Transactions d={d} />}
    </div>
  );
}

type ScoutingResult = ApiResult<Report> | null;

function Overview({ d, scouting }: { d: Detail; scouting: ScoutingResult }) {
  const t = d.team;
  const league = d.league;
  const order = Object.keys(DIMENSION_TITLES);
  const classes = [...d.classifications].sort((a, b) => order.indexOf(a.dimension) - order.indexOf(b.dimension));
  const comp = compositionSeries(d.weekly_composition, POS);
  const cell = (p: number, pos: string) => d.weekly_composition.find((r) => r.scoring_period === p && r.position === pos);

  return (
    <>
      <StatStrip>
        <Stat label="Record" value={`${t.wins}–${t.losses}${t.ties ? `–${t.ties}` : ""}`} note={t.playoff_seed ? `seed ${t.playoff_seed}` : "no seed yet"} />
        <Stat label="Points for" value={num(t.points_for, 1)} note={`${num(t.points_against, 1)} against`} />
        <Stat label="Acquisitions / drops" value={`${t.acquisitions ?? "—"} / ${t.drops ?? "—"}`} note="ESPN counters" />
        <Stat label="Trades" value={t.trades ?? "—"} note={t.move_to_ir !== null ? `${t.move_to_ir} IR moves` : undefined} />
        <Stat
          label={league.is_faab ? "FAAB spent" : "Waiver priority"}
          value={league.is_faab ? `$${t.faab_spent ?? 0}` : `#${t.waiver_rank ?? "?"}`}
          note={league.is_faab ? `of $${league.faab_budget ?? "?"}` : `of ${league.size} teams`}
        />
      </StatStrip>

      {scouting?.ok ? <ScoutingSummary report={scouting.data} />
        : scouting ? <Notice tone="negative" title="Scouting summary unavailable">{scouting.error}</Notice> : null}

      <Section
        title="Stored tendencies and rules"
        flush
        footnote="League-relative labels with the sample behind each: n against the minimum history required. A label with too little history is insufficient, not low."
      >
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Tendency</th><th>Label</th><th className="num">Value</th><th className="num">League mean</th><th className="num">z</th>
                <th className="num">Sample</th><th>Confidence</th><th>Rule</th>
              </tr>
            </thead>
            <tbody>
              {classes.map((c) => (
                <tr key={c.dimension}>
                  <td>{DIMENSION_TITLES[c.dimension] ?? c.dimension} <span className="faint mono">{c.metric}</span></td>
                  <td><Badge value={c.label} tone={labelTone(c.label)} /></td>
                  <td className="num">{formatMetric(c.metric, c.value)}</td>
                  <td className="num">{formatMetric(c.metric, c.league_mean)}</td>
                  <td className="num">{num(c.z_score, 2, "Not z-scored")}</td>
                  <td className="num">{c.sample_size} <span className="faint">/ min {c.min_sample}</span></td>
                  <td><ConfDot level={c.confidence} withText /></td>
                  <td className={styles.rule}>{c.rule}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      <div className="grid grid-2">
        <Section title="Roster construction by week" footnote="Players rostered per position at each scoring period. A position with no rostered player that week counts as zero.">
          <LineChart
            series={comp.series.map((s) => ({ id: s.position, label: s.position, points: s.points }))}
            xLabels={comp.periods.map(String)}
            markers
            yFormat={(v) => String(Math.round(v))}
            ariaLabel="Players rostered per position by week"
          />
        </Section>
        <Section title="Weekly roster counts" flush footnote="Rostered players per position; starters in parentheses.">
          {comp.periods.length === 0 ? <EmptyState title="No weekly roster history" /> : (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Week</th>{POS.map((p) => <th key={p} className="num">{p}</th>)}</tr></thead>
                <tbody>
                  {comp.periods.map((p) => (
                    <tr key={p}>
                      <td>{p}</td>
                      {POS.map((pos) => {
                        const r = cell(p, pos);
                        return <td key={pos} className="num">{r ? <>{r.n} <span className="faint">({r.starters} st)</span></> : 0}</td>;
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Section>
      </div>

      <div className="grid grid-2">
        <Section title="Positional needs (current roster)" flush>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Pos</th><th title="Positional need, 0–10 scale (not a player rating)">Need</th><th className="num">Healthy</th><th className="num">Out</th><th className="num" title="Internal 0–10 starter value (not the 0–100 value rating)">Starter value</th></tr></thead>
              <tbody>
                {d.needs.filter((n) => n.starters_required > 0).map((n) => (
                  <tr key={n.position}>
                    <td><Pos value={n.position} /></td>
                    <td><Score value={n.need_score * 10} /></td>
                    <td className="num">{n.healthy}</td><td className="num">{n.unavailable}</td><td className="num">{num(n.starter_value)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>

        <Section title="Current roster" flush>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Slot</th><th>Player</th><th>Status</th><th>Signal</th><th className="num" title={`Value rating, 0–100: ${RATING_DESCRIPTION.value}. ${RATING_CAVEAT}`}>Value</th></tr></thead>
              <tbody>
                {d.roster.map((p) => (
                  <tr key={p.espn_player_id}>
                    <td className="slot">{p.lineup_slot}</td>
                    <td><PlayerLink id={p.espn_player_id} name={p.full_name} /> <Pos value={p.position} /></td>
                    <td>{p.injury_status && !["ACTIVE", "NORMAL"].includes(p.injury_status) ? <Badge value={p.injury_status} /> : ""}</td>
                    <td><Badge value={p.market_signal} /></td>
                    <td className="num"><Rating rating={ratingFor(p, "value")} kind="value" variant="text" /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      </div>

      <Method title="All measured metrics">
        <div className={styles.metrics}>
          <table>
            <thead><tr><th>Metric</th><th className="num">Value</th><th className="num">Num / den</th><th className="num">n</th><th>Definition</th></tr></thead>
            <tbody>
              {d.metrics.map((m) => (
                <tr key={m.metric}>
                  <td className="mono">{m.metric}</td>
                  <td className="num">{formatMetric(m.metric, m.value)}</td>
                  <td className="num faint">{m.numerator !== null && m.denominator !== null ? `${num(m.numerator, 0)} / ${num(m.denominator, 0)}` : ""}</td>
                  <td className="num faint">{m.sample_size ?? ""}</td>
                  <td className={styles.rule}>{m.description}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Method>
    </>
  );
}

function Transactions({ d }: { d: Detail }) {
  return (
    <>
      {d.failed_claims.length > 0 && (
        <Section title="Unsuccessful waiver claims" flush footnote="Visible in ESPN's transaction log.">
          <div className="table-wrap">
            <table>
              <thead><tr><th>Date</th><th>Wk</th><th>Player</th><th>Status</th><th className="num">Bid</th></tr></thead>
              <tbody>
                {d.failed_claims.map((f) => (
                  <tr key={f.transaction_id}>
                    <td>{date(f.date)}</td><td>{f.scoring_period}</td>
                    <td>{f.full_name} {f.position && <Pos value={f.position} />}</td>
                    <td className="faint">{f.status}</td><td className="num">{f.bid_amount ? `$${f.bid_amount}` : ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}
      <Section title="Transaction history" flush={d.moves.length > 0} footnote="Adds, drops and trades as recorded by ESPN; the evidence behind the tendencies.">
        {d.moves.length === 0
          ? <EmptyState title="No transactions recorded">Adds, drops and trades appear here after the next ESPN sync.</EmptyState>
          : <MovesTable moves={d.moves} showTeam={false} />}
      </Section>
    </>
  );
}
