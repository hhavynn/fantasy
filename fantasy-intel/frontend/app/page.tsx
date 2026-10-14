import Link from "next/link";
import { MovesTable } from "@/components/MovesTable";
import { PlayerLink } from "@/components/PlayerLink";
import { Badge, Callout, ErrorBox, Pos, Score, TrendArrow } from "@/components/ui";
import { api } from "@/lib/api";
import { num } from "@/lib/format";
import type { League, Move, Need, RosterPlayer, SyncInfo, Team } from "@/lib/types";

type Overview = {
  league: League; my_team: Team | null; standings: Team[]; roster: RosterPlayer[]; injuries: RosterPlayer[];
  needs: Need[]; recent_transactions: Move[]; sync: SyncInfo; nfl_latest_week: number | null;
};

function SetupHelp({ error }: { error: string }) {
  return (
    <>
      <ErrorBox error={error} />
      <Callout>
        <b>First run?</b> Fill in <code>.env</code> (copy <code>.env.example</code>), then from <code>backend/</code> run{" "}
        <code>uv run python ../scripts/sync_espn.py</code> and <code>uv run python ../scripts/sync_nfl.py</code>.
      </Callout>
    </>
  );
}

export default async function OverviewPage() {
  const res = await api<Overview>("/api/overview");
  if (!res.ok) return <SetupHelp error={res.error} />;
  const o = res.data;
  const me = o.my_team;
  const starters = o.roster.filter((p) => p.is_starter);
  const bench = o.roster.filter((p) => !p.is_starter);
  const rank = me ? o.standings.findIndex((t) => t.team_id === me.team_id) + 1 : null;

  return (
    <div className="stack">
      <div className="page-head">
        <h1>{me ? me.name : "Your team"}</h1>
        <span className="sub">
          {o.league.name} · {o.league.season} · week {o.league.current_week} · {o.league.size} teams ·{" "}
          {o.league.reception_points === 1 ? "PPR" : o.league.reception_points ? `${o.league.reception_points} PPR` : "scoring ?"}
          {o.league.is_faab ? ` · FAAB $${o.league.faab_budget}` : " · waiver priority"}
        </span>
      </div>

      {!me && <Callout>Your team wasn&apos;t identified from SWID. Set <code>ESPN_MY_TEAM_ID</code> in <code>.env</code> and re-sync.</Callout>}

      {me && (
        <div className="grid grid-4">
          <div className="stat"><div className="label">Record</div><div className="value">{me.wins}-{me.losses}{me.ties ? `-${me.ties}` : ""}</div></div>
          <div className="stat"><div className="label">Standing</div><div className="value">{rank} / {o.standings.length}</div></div>
          <div className="stat"><div className="label">Points for</div><div className="value">{num(me.points_for, 1)}</div></div>
          <div className="stat"><div className="label">Points against</div><div className="value">{num(me.points_against, 1)}</div></div>
          <div className="stat">
            <div className="label">{o.league.is_faab ? "FAAB left" : "Waiver priority"}</div>
            <div className="value">
              {o.league.is_faab ? `$${(o.league.faab_budget ?? 0) - (me.faab_spent ?? 0)}` : `#${me.waiver_rank ?? "?"}`}
            </div>
          </div>
        </div>
      )}

      <div className="grid grid-2">
        <section className="panel">
          <h2>Current roster</h2>
          <RosterTable rows={starters} title="Starters" />
          <div style={{ height: 10 }} />
          <RosterTable rows={bench} title="Bench / IR" />
        </section>

        <div className="stack">
          <section className="panel">
            <h2>Biggest roster needs</h2>
            <table>
              <thead>
                <tr><th>Pos</th><th className="num">Need</th><th className="num">Healthy / target</th><th className="num">Out</th><th className="num">Starter value</th><th className="num">League avg</th></tr>
              </thead>
              <tbody>
                {o.needs.filter((n) => n.starters_required > 0).map((n) => (
                  <tr key={n.position}>
                    <td><Pos value={n.position} /></td>
                    <td><Score value={n.need_score * 10} /></td>
                    <td className="num">{n.healthy} / {n.details.depth_target}</td>
                    <td className="num">{n.unavailable}</td>
                    <td className="num">{num(n.starter_value)}</td>
                    <td className="num">{num(n.details.league_avg_starter_value)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="faint" style={{ marginTop: 8 }}>
              Need = 0.55·shortage of healthy bodies + 0.45·gap between your starters&apos; value and the league-average starter.
            </p>
          </section>

          <section className="panel">
            <h2>Injuries on my roster</h2>
            {o.injuries.length === 0 ? <p className="faint">No injury designations on your roster.</p> : (
              <table>
                <thead><tr><th>Player</th><th>ESPN</th><th>NFL report</th><th>Injury</th></tr></thead>
                <tbody>
                  {o.injuries.map((p) => (
                    <tr key={p.espn_player_id}>
                      <td><PlayerLink id={p.espn_player_id} name={p.full_name} /> <Pos value={p.position} /></td>
                      <td><Badge value={p.injury_status} /></td>
                      <td>{p.nfl_injury ? <><Badge value={p.nfl_injury.report_status ?? "none"} /> <span className="faint">wk {p.nfl_injury.week}</span></> : <span className="missing" title="No NFL injury report row">—</span>}</td>
                      <td>{p.nfl_injury?.report_primary_injury ?? ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          <section className="panel">
            <h2>Standings</h2>
            <table>
              <thead><tr><th>#</th><th>Team</th><th>W-L</th><th className="num">PF</th><th className="num">PA</th></tr></thead>
              <tbody>
                {o.standings.map((t, i) => (
                  <tr key={t.team_id} className={t.is_mine ? "mine" : ""}>
                    <td>{i + 1}</td>
                    <td><Link href={`/managers/${t.team_id}`}>{t.name}</Link></td>
                    <td>{t.wins}-{t.losses}{t.ties ? `-${t.ties}` : ""}</td>
                    <td className="num">{num(t.points_for, 1)}</td>
                    <td className="num">{num(t.points_against, 1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </div>
      </div>

      <section className="panel">
        <h2>Recent league transactions</h2>
        <MovesTable moves={o.recent_transactions} />
      </section>
    </div>
  );
}

function RosterTable({ rows, title }: { rows: RosterPlayer[]; title: string }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>{title}</th><th>Player</th><th>NFL</th><th>Status</th><th title="Season PPR/game (nflverse)" className="num">PPR/g</th>
            <th title="Expected PPR/game from role (nflverse xFP)" className="num">xFP/g</th><th>Trend</th><th title="Underlying-value classification">Class</th>
            <th className="num" title="0-10 rest-of-season value proxy">Value</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((p) => (
            <tr key={p.espn_player_id}>
              <td className="faint">{p.lineup_slot}</td>
              <td><PlayerLink id={p.espn_player_id} name={p.full_name} /> <Pos value={p.position} /></td>
              <td>{p.pro_team ?? "FA"}{p.on_bye ? <> <Badge value="BYE" tone="warn" /></> : null}</td>
              <td>{p.injury_status && !["ACTIVE", "NORMAL"].includes(p.injury_status) ? <Badge value={p.injury_status} /> : ""}</td>
              <td className="num">{num(p.fp_per_game, 1, p.gsis_id ? "No games" : "No NFL id match")}</td>
              <td className="num">{num(p.xfp_per_game, 1, p.gsis_id ? "No games" : "No NFL id match")}</td>
              <td><TrendArrow label={p.trend_label} /></td>
              <td><Badge value={p.classification} /></td>
              <td className="num">{num(p.value_score)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
