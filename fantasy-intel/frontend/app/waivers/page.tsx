import Link from "next/link";
import { PlayerLink } from "@/components/PlayerLink";
import { Badge, ErrorBox, Pos, Score, TrendArrow } from "@/components/ui";
import { api } from "@/lib/api";
import { label, num, signed, when } from "@/lib/format";
import type { League } from "@/lib/types";

type Competitor = { team_id: number; name: string; interest: number; reasons: string[] };
type WaiverRow = {
  espn_player_id: number; full_name: string; position: string; pro_team: string | null; availability_status: string;
  injury_status: string | null; percent_owned: number | null; percent_change: number | null;
  opportunity_score: number | null; roster_fit_score: number; league_interest: string; interest_score: number;
  waiver_score: number; recommendation: string; suggested_drop_id: number | null; drop_name: string | null;
  drop_position: string | null; trend_label: string | null; classification: string | null; games: number | null;
  xfp_per_game: number | null; fp_per_game: number | null; reasons: string[]; competitors: Competitor[];
  components: {
    player: { basis: string; base_opportunity: number | null; trend_adj: number; injury_adj: number; value: number | null };
    roster: { need: number; my_starter_value: number | null; upgrade: number; bench_upgrade?: number; value_margin_vs_drop: number | null };
    league: { attractiveness: number; max_manager_interest: number; managers_interested: number; scarcity_bonus: number };
    formula: string;
  };
};

const POSITIONS = ["ALL", "QB", "RB", "WR", "TE", "K", "D/ST"];
const RECS = ["ALL", "CLAIM", "WAIT", "IGNORE"];

function href(pos: string, rec: string) {
  const qs = new URLSearchParams();
  if (pos !== "ALL") qs.set("pos", pos);
  if (rec !== "ALL") qs.set("rec", rec);
  const s = qs.toString();
  return s ? `/waivers?${s}` : "/waivers";
}

export default async function WaiversPage({ searchParams }: PageProps<"/waivers">) {
  const sp = await searchParams;
  const pos = typeof sp.pos === "string" ? sp.pos : "ALL";
  const rec = typeof sp.rec === "string" ? sp.rec : "ALL";
  const qs = new URLSearchParams();
  if (pos !== "ALL") qs.set("position", pos);
  if (rec !== "ALL") qs.set("recommendation", rec);
  const res = await api<{ league: League; players: WaiverRow[]; computed_at: string | null }>(`/api/waivers?${qs}`);
  if (!res.ok) return <ErrorBox error={res.error} />;
  const { players, computed_at } = res.data;

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Waiver intelligence</h1>
        <span className="sub">Available players ranked by local waiver score · computed {when(computed_at)}</span>
      </div>
      <div className="filters">
        {POSITIONS.map((p) => <Link key={p} className={`chip ${p === pos ? "active" : ""}`} href={href(p, rec)}>{p}</Link>)}
        <span style={{ width: 16 }} />
        {RECS.map((r) => <Link key={r} className={`chip ${r === rec ? "active" : ""}`} href={href(pos, r)}>{r}</Link>)}
      </div>
      <p className="faint">
        waiver score = 0.55·opportunity + 0.45·roster fit (+ scarcity bonus). CLAIM needs score ≥ 6.0, roster fit ≥ 3.0 and must beat your suggested drop by ≥ 0.5 value;
        WAIT ≥ 4.5. League interest is a LOW/MEDIUM/HIGH bucket, not a probability.
      </p>
      <section className="panel">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Rec</th><th>Player</th><th>Status</th><th className="num">% own</th><th className="num">Opportunity</th><th>Role trend</th>
                <th className="num">PPR/g</th><th className="num">xFP/g</th><th className="num">Roster fit</th><th>League competition</th>
                <th>Suggested drop</th><th className="num">Score</th><th>Why</th>
              </tr>
            </thead>
            <tbody>
              {players.map((w) => (
                <tr key={w.espn_player_id}>
                  <td><Badge value={w.recommendation} /></td>
                  <td>
                    <PlayerLink id={w.espn_player_id} name={w.full_name} /> <Pos value={w.position} /> <span className="faint">{w.pro_team}</span>
                    {w.injury_status && !["ACTIVE", "NORMAL"].includes(w.injury_status) && <> <Badge value={w.injury_status} /></>}
                  </td>
                  <td className="faint">{label(w.availability_status)}</td>
                  <td className="num">{num(w.percent_owned, 1)} <span className="faint">{signed(w.percent_change)}</span></td>
                  <td><Score value={w.opportunity_score} /></td>
                  <td><TrendArrow label={w.trend_label} /> <Badge value={w.classification} /></td>
                  <td className="num">{num(w.fp_per_game)}</td>
                  <td className="num">{num(w.xfp_per_game)}</td>
                  <td><Score value={w.roster_fit_score} /></td>
                  <td>
                    <Badge value={w.league_interest} />{" "}
                    <span className="faint">{w.competitors.slice(0, 2).map((c) => c.name).join(", ")}</span>
                  </td>
                  <td>{w.suggested_drop_id ? <><PlayerLink id={w.suggested_drop_id} name={w.drop_name} /> <Pos value={w.drop_position} /></> : <span className="faint">open spot</span>}</td>
                  <td className="num">{w.waiver_score.toFixed(2)}</td>
                  <td className="wrap">
                    <details>
                      <summary>{w.reasons[0]}</summary>
                      <ul className="reasons">{w.reasons.slice(1).map((r, i) => <li key={i}>{r}</li>)}</ul>
                      <div className="kv faint" style={{ marginTop: 6 }}>
                        <span className="k">Player</span>
                        <span>basis {w.components.player.basis}, base opp {num(w.components.player.base_opportunity)}, trend {signed(w.components.player.trend_adj, 0)}, injury {signed(w.components.player.injury_adj, 0)}, value {num(w.components.player.value)}</span>
                        <span className="k">Roster</span>
                        <span>need {w.components.roster.need.toFixed(2)}, my starters {num(w.components.roster.my_starter_value)}, upgrade {w.components.roster.upgrade.toFixed(2)}, vs drop {signed(w.components.roster.value_margin_vs_drop)}</span>
                        <span className="k">League</span>
                        <span>attractiveness {w.components.league.attractiveness.toFixed(2)}, top manager interest {w.components.league.max_manager_interest.toFixed(2)}, {w.components.league.managers_interested} interested</span>
                      </div>
                      {w.competitors.length > 0 && (
                        <ol className="reasons">
                          {w.competitors.map((c) => (
                            <li key={c.team_id}><Link href={`/managers/${c.team_id}`}>{c.name}</Link> · interest {c.interest.toFixed(2)}{c.reasons.length ? `: ${c.reasons.join("; ")}` : ""}</li>
                          ))}
                        </ol>
                      )}
                    </details>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {players.length === 0 && <p className="faint">No available players match. Run the ESPN + NFL syncs first.</p>}
      </section>
    </div>
  );
}
