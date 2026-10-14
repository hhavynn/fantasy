import Link from "next/link";
import { Badge, ErrorBox } from "@/components/ui";
import { api } from "@/lib/api";
import { ClassBadge, ConfDot } from "@/lib/managers";
import type { Classification, League, Metric, Team } from "@/lib/types";

type ManagerCard = Team & {
  classifications: Record<string, Classification>;
  metrics: Record<string, Metric>;
  composition: Record<string, number>;
};

const CARD_ROWS: [string, string][] = [
  ["waiver_aggression", "Waiver aggression"],
  ["roster_churn", "Roster churn"],
  ["rb_interest", "RB interest"],
  ["wr_interest", "WR interest"],
  ["te_interest", "TE interest"],
  ["qb_streaming", "QB streaming"],
  ["te_streaming", "TE streaming"],
  ["dst_streaming", "D/ST streaming"],
  ["trade_activity", "Trade activity"],
  ["injury_response", "Injury replacement"],
];

export default async function ManagersPage() {
  const res = await api<{ league: League; managers: ManagerCard[] }>("/api/managers");
  if (!res.ok) return <ErrorBox error={res.error} />;
  const { managers, league } = res.data;
  const others = [...managers].sort((a, b) => Number(a.is_mine) - Number(b.is_mine));

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Manager tendencies</h1>
        <span className="sub">
          Measured from {league.season} transactions and weekly rosters (through week {league.current_week}). Labels are league-relative;
          hover a label for the rule, league mean and sample size. <ConfDot level="HIGH" /> high <ConfDot level="MEDIUM" /> medium{" "}
          <ConfDot level="LOW" /> low confidence.
        </span>
      </div>
      <div className="grid grid-3">
        {others.map((m) => {
          const adds = m.metrics.adds_total?.value ?? 0;
          const drops = m.metrics.drops_total?.value ?? 0;
          const trades = m.metrics.trade_count?.value ?? 0;
          const c = m.composition;
          return (
            <Link key={m.team_id} href={`/managers/${m.team_id}`} className="panel mgr-card">
              <div className="row-flex" style={{ justifyContent: "space-between" }}>
                <h3 style={{ margin: 0 }}>{m.name} {m.is_mine && <Badge value="YOU" tone="info" />}</h3>
                <span className="mono">{m.wins}-{m.losses}{m.ties ? `-${m.ties}` : ""}</span>
              </div>
              <div className="faint" style={{ marginBottom: 8 }}>{m.owner_names ?? "owner unknown"}</div>
              <div className="faint mono" style={{ marginBottom: 8 }} title="Current roster construction">
                QB {c.QB ?? 0} · RB {c.RB ?? 0} · WR {c.WR ?? 0} · TE {c.TE ?? 0} · K {c.K ?? 0} · D/ST {c["D/ST"] ?? 0}
              </div>
              <div className="faint" style={{ marginBottom: 8 }}>
                {adds} adds · {drops} drops · {trades} trades
                {league.is_faab ? ` · $${m.faab_spent ?? 0} FAAB spent` : ` · waiver #${m.waiver_rank ?? "?"}`}
              </div>
              <div className="kv">
                {CARD_ROWS.map(([dim, title]) => (
                  <span key={dim} style={{ display: "contents" }}>
                    <span className="k">{title}</span>
                    <ClassBadge c={m.classifications[dim]} />
                  </span>
                ))}
              </div>
            </Link>
          );
        })}
      </div>
    </div>
  );
}
