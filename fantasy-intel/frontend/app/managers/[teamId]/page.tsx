import Link from "next/link";
import { MovesTable } from "@/components/MovesTable";
import { PlayerLink } from "@/components/PlayerLink";
import { Badge, ErrorBox, Pos, Score } from "@/components/ui";
import { api } from "@/lib/api";
import { date, num } from "@/lib/format";
import { ConfDot, DIMENSION_TITLES, formatMetric } from "@/lib/managers";
import type { Classification, League, Metric, Move, Need, RosterPlayer, Team } from "@/lib/types";

type Detail = {
  league: League; team: Team; metrics: Metric[]; classifications: Classification[]; roster: RosterPlayer[];
  weekly_composition: { scoring_period: number; position: string; n: number; starters: number }[];
  needs: Need[]; moves: Move[];
  failed_claims: { transaction_id: string; status: string; scoring_period: number; bid_amount: number | null; date: string | null; full_name: string | null; position: string | null }[];
};

const POS = ["QB", "RB", "WR", "TE", "K", "D/ST"];

export default async function ManagerDetail({ params }: PageProps<"/managers/[teamId]">) {
  const { teamId } = await params;
  const res = await api<Detail>(`/api/managers/${teamId}`);
  if (!res.ok) return <ErrorBox error={res.error} />;
  const d = res.data;
  const t = d.team;
  const order = Object.keys(DIMENSION_TITLES);
  const classes = [...d.classifications].sort((a, b) => order.indexOf(a.dimension) - order.indexOf(b.dimension));
  const periods = [...new Set(d.weekly_composition.map((r) => r.scoring_period))].sort((a, b) => a - b);
  const comp = (p: number, pos: string) => d.weekly_composition.find((r) => r.scoring_period === p && r.position === pos);

  return (
    <div className="stack">
      <div className="page-head">
        <h1>{t.name}</h1>
        {t.is_mine && <Badge value="YOU" tone="info" />}
        <span className="sub">{t.owner_names} · {t.wins}-{t.losses}{t.ties ? `-${t.ties}` : ""} · PF {num(t.points_for, 1)}</span>
        <Link href="/managers">← all managers</Link>
      </div>

      <section className="panel">
        <h2>Classifications &amp; evidence</h2>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Tendency</th><th>Label</th><th className="num">Value</th><th className="num">League mean</th><th className="num">z</th>
              <th className="num">Sample</th><th>Confidence</th><th>Rule</th></tr></thead>
            <tbody>
              {classes.map((c) => (
                <tr key={c.dimension}>
                  <td>{DIMENSION_TITLES[c.dimension] ?? c.dimension} <span className="faint mono">{c.metric}</span></td>
                  <td><Badge value={c.label} tone={c.label === "HIGH" ? "info" : c.label === "MEDIUM" ? "warn" : ""} /></td>
                  <td className="num">{formatMetric(c.metric, c.value)}</td>
                  <td className="num">{formatMetric(c.metric, c.league_mean)}</td>
                  <td className="num">{num(c.z_score, 2, "Not z-scored")}</td>
                  <td className="num">{c.sample_size} <span className="faint">/ min {c.min_sample}</span></td>
                  <td><ConfDot level={c.confidence} /> {c.confidence}</td>
                  <td className="wrap faint">{c.rule}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <div className="grid grid-2">
        <section className="panel">
          <h2>All measured metrics</h2>
          <table>
            <thead><tr><th>Metric</th><th className="num">Value</th><th className="num">Num / den</th><th className="num">n</th><th>Definition</th></tr></thead>
            <tbody>
              {d.metrics.map((m) => (
                <tr key={m.metric}>
                  <td className="mono">{m.metric}</td>
                  <td className="num">{formatMetric(m.metric, m.value)}</td>
                  <td className="num faint">{m.numerator !== null && m.denominator !== null ? `${num(m.numerator, 0)} / ${num(m.denominator, 0)}` : ""}</td>
                  <td className="num faint">{m.sample_size ?? ""}</td>
                  <td className="wrap faint">{m.description}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <div className="stack">
          <section className="panel">
            <h2>Roster construction by week</h2>
            <table>
              <thead><tr><th>Week</th>{POS.map((p) => <th key={p} className="num">{p}</th>)}</tr></thead>
              <tbody>
                {periods.map((p) => (
                  <tr key={p}>
                    <td>{p}</td>
                    {POS.map((pos) => {
                      const r = comp(p, pos);
                      return <td key={pos} className="num">{r ? <>{r.n} <span className="faint">({r.starters} st)</span></> : 0}</td>;
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          <section className="panel">
            <h2>Positional needs (current roster)</h2>
            <table>
              <thead><tr><th>Pos</th><th className="num">Need</th><th className="num">Healthy</th><th className="num">Out</th><th className="num">Starter value</th></tr></thead>
              <tbody>
                {d.needs.filter((n) => n.starters_required > 0).map((n) => (
                  <tr key={n.position}><td><Pos value={n.position} /></td><td><Score value={n.need_score * 10} /></td>
                    <td className="num">{n.healthy}</td><td className="num">{n.unavailable}</td><td className="num">{num(n.starter_value)}</td></tr>
                ))}
              </tbody>
            </table>
          </section>

          <section className="panel">
            <h2>Current roster</h2>
            <table>
              <tbody>
                {d.roster.map((p) => (
                  <tr key={p.espn_player_id}>
                    <td className="faint">{p.lineup_slot}</td>
                    <td><PlayerLink id={p.espn_player_id} name={p.full_name} /> <Pos value={p.position} /></td>
                    <td>{p.injury_status && !["ACTIVE", "NORMAL"].includes(p.injury_status) ? <Badge value={p.injury_status} /> : ""}</td>
                    <td><Badge value={p.market_signal} /></td>
                    <td className="num">{num(p.value_score)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </div>
      </div>

      {d.failed_claims.length > 0 && (
        <section className="panel">
          <h2>Unsuccessful waiver claims (visible in ESPN)</h2>
          <table>
            <thead><tr><th>Date</th><th>Wk</th><th>Player</th><th>Status</th><th className="num">Bid</th></tr></thead>
            <tbody>
              {d.failed_claims.map((f) => (
                <tr key={f.transaction_id}><td>{date(f.date)}</td><td>{f.scoring_period}</td><td>{f.full_name} {f.position && <Pos value={f.position} />}</td>
                  <td className="faint">{f.status}</td><td className="num">{f.bid_amount ? `$${f.bid_amount}` : ""}</td></tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      <section className="panel">
        <h2>Transaction history (evidence)</h2>
        <MovesTable moves={d.moves} showTeam={false} />
      </section>
    </div>
  );
}
