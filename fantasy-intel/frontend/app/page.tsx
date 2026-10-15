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
  const rank = me ? o.standings.findIndex((t) => t.team_id === me.team_id) + 1 : null;
  const n = o.standings.length;
  const rankBy = (k: "points_for" | "points_against") =>
    me ? [...o.standings].sort((a, b) => (b[k] ?? 0) - (a[k] ?? 0)).findIndex((t) => t.team_id === me.team_id) + 1 : null;
  const scoring = o.league.reception_points === 1 ? "PPR" : o.league.reception_points ? `${o.league.reception_points} PPR` : "Scoring ?";

  return (
    <div className="stack">
      <div className="page-head">
        <h1>{me ? me.name : "Your team"}</h1>
        <span className="sub">
          {o.league.name} · {o.league.season} · week {o.league.current_week} · {o.league.size} teams · {scoring}
          {o.league.is_faab ? ` · FAAB $${o.league.faab_budget}` : " · waiver priority"}
        </span>
      </div>

      {!me && <Callout>Your team wasn&apos;t identified from SWID. Set <code>ESPN_MY_TEAM_ID</code> in <code>.env</code> and re-sync.</Callout>}

      {me && (
        <div className="grid grid-4">
          <div className="stat">
            <div className="label">Record</div>
            <div className="value">{me.wins}–{me.losses}{me.ties ? `–${me.ties}` : ""}</div>
            <div className="note">{rank ? `${ordinal(rank)} of ${n}` : ""}</div>
          </div>
          <div className="stat">
            <div className="label">Points for</div>
            <div className="value">{num(me.points_for, 1)}</div>
            <div className="note">{rankBy("points_for") ? `${ordinal(rankBy("points_for")!)} most in league` : ""}</div>
          </div>
          <div className="stat">
            <div className="label">Points against</div>
            <div className="value">{num(me.points_against, 1)}</div>
            <div className="note">{rankBy("points_against") ? `${ordinal(rankBy("points_against")!)} most in league` : ""}</div>
          </div>
          <div className="stat">
            <div className="label">{o.league.is_faab ? "FAAB left" : "Waiver priority"}</div>
            <div className="value">
              {o.league.is_faab ? `$${(o.league.faab_budget ?? 0) - (me.faab_spent ?? 0)}` : `#${me.waiver_rank ?? "?"}`}
            </div>
            <div className="note">{o.league.is_faab ? `of $${o.league.faab_budget ?? "?"}` : `of ${n} teams`}</div>
          </div>
          <div className="stat">
            <div className="label">Injury designations</div>
            <div className="value">{o.injuries.length}</div>
            <div className="note">on your roster</div>
          </div>
        </div>
      )}

      <div className="grid-main">
        <section className="panel">
          <h2>Current roster</h2>
          <RosterTable rows={o.roster} />
        </section>

        <div className="stack">
          <section className="panel">
            <h2>Biggest roster needs</h2>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Pos</th><th>Need</th>
                    <th className="num" title="Availability-weighted bodies vs depth target">Avail</th>
                    <th className="num" title="Your starters' value at the position vs the league average starter">Value / avg</th>
                  </tr>
                </thead>
                <tbody>
                  {o.needs.filter((x) => x.starters_required > 0).map((x) => (
                    <tr key={x.position}>
                      <td><Pos value={x.position} /></td>
                      <td><Score value={x.need_score * 10} /></td>
                      <td className="num" title={`${x.starters_required} start${x.starters_required === 1 ? "" : "s"}${x.details.flex_slots_filled ? " incl. FLEX" : ""} · ${x.unavailable} out`}>
                        {avail(x.effective_available ?? x.healthy)} / {x.details.depth_target}
                      </td>
                      <td className="num">{num(x.starter_value)} <span className="faint">/ {num(x.details.league_avg_starter_value)}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <details>
              <summary>How need is calculated</summary>
              <p className="faint">
                Need = 0.55·shortage of availability-weighted bodies + 0.45·gap between your starters&apos; value at the position (fixed and FLEX-assigned) and the league
                average. FLEX is one real slot filled by the best eligible player. Weights: available 1, questionable 0.75, doubtful 0.15, out/IR 0.
              </p>
            </details>
          </section>

          <section className="panel">
            <h2>Injuries on my roster</h2>
            {o.injuries.length === 0 ? <p className="faint">No injury designations on your roster.</p> : (
              <div className="table-wrap">
                <table>
                  <thead><tr><th>Player</th><th>ESPN</th><th>NFL report</th></tr></thead>
                  <tbody>
                    {o.injuries.map((p) => (
                      <tr key={p.espn_player_id}>
                        <td><PlayerLink id={p.espn_player_id} name={p.full_name} /> <Pos value={p.position} /></td>
                        <td><Badge value={p.injury_status} /></td>
                        <td>
                          {p.nfl_injury
                            ? <><Badge value={p.nfl_injury.report_status ?? "none"} /> <span className="faint">wk {p.nfl_injury.week}{p.nfl_injury.report_primary_injury ? ` · ${p.nfl_injury.report_primary_injury}` : ""}</span></>
                            : <span className="missing" title="No NFL injury report row">—</span>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="panel">
            <h2>Standings</h2>
            <div className="table-wrap">
              <table>
                <thead><tr><th className="num">#</th><th>Team</th><th>W–L</th><th className="num">PF</th><th className="num">PA</th></tr></thead>
                <tbody>
                  {o.standings.map((t, i) => (
                    <tr key={t.team_id} className={t.is_mine ? "mine" : ""}>
                      <td className="num faint">{i + 1}</td>
                      <td><Link href={`/managers/${t.team_id}`}>{t.name}</Link></td>
                      <td>{t.wins}–{t.losses}{t.ties ? `–${t.ties}` : ""}</td>
                      <td className="num">{num(t.points_for, 1)}</td>
                      <td className="num">{num(t.points_against, 1)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
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

function avail(v: number | null | undefined) {
  if (v === null || v === undefined) return <span className="missing" title="Not computed">—</span>;
  return Number.isInteger(v) ? String(v) : v.toFixed(1);
}

function ordinal(n: number): string {
  const v = n % 100;
  return `${n}${v >= 11 && v <= 13 ? "th" : ["th", "st", "nd", "rd"][n % 10] ?? "th"}`;
}

function RosterTable({ rows }: { rows: RosterPlayer[] }) {
  const groups: [string, RosterPlayer[]][] = [
    ["Starters", rows.filter((p) => p.is_starter)],
    ["Bench / IR", rows.filter((p) => !p.is_starter)],
  ];
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Slot</th><th>Player</th><th>NFL</th><th>Status</th><th title="Season PPR/game (nflverse)" className="num">PPR/g</th>
            <th title="Expected PPR/game from role (nflverse xFP)" className="num">xFP/g</th><th>Trend</th><th title="Market signal (BUY needs a meaningful absolute role)">Signal</th>
            <th title="0-10 rest-of-season value proxy">Value</th>
          </tr>
        </thead>
        {groups.map(([title, ps]) => (
          <tbody key={title}>
            <tr className="group"><td colSpan={9}>{title}</td></tr>
            {ps.map((p) => (
              <tr key={p.espn_player_id}>
                <td className="slot">{p.lineup_slot}</td>
                <td><PlayerLink id={p.espn_player_id} name={p.full_name} /> <Pos value={p.position} /></td>
                <td>{p.pro_team ?? "FA"}{p.on_bye ? <> <Badge value="BYE" tone="warn" /></> : null}</td>
                <td>{p.availability && p.availability.status !== "AVAILABLE"
                  ? <Badge value={p.availability.status} title={`${p.availability.sources.join(", ")} · availability weight ${p.availability.weight}`} /> : ""}</td>
                <td className="num">{num(p.fp_per_game, 1, p.gsis_id ? "No games" : "No NFL id match")}</td>
                <td className="num">{num(p.xfp_per_game, 1, p.gsis_id ? "No games" : "No NFL id match")}</td>
                <td><TrendArrow label={p.trend_label} /></td>
                <td><Badge value={p.market_signal} /></td>
                <td><Score value={p.value_score} /></td>
              </tr>
            ))}
          </tbody>
        ))}
      </table>
    </div>
  );
}
