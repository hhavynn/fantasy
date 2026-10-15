import Link from "next/link";
import { PlayerLink } from "@/components/PlayerLink";
import { Badge, Callout, ErrorBox, Pos } from "@/components/ui";
import { api } from "@/lib/api";
import { when } from "@/lib/format";
import type { ScheduleIntelligence, SiLabel, SiMetric, SiPlayer, SiWeek } from "@/lib/types";

// Schedule intelligence: each remaining week's opponent and what that defense has allowed in games
// completed before the week. Descriptive only: no projection, no points adjustment, no composite score.

const TONE: Record<SiLabel, string> = {
  permissive: "good", middle: "", tougher: "bad", insufficient: "warn", unavailable: "warn", bye: "info",
};
const SHORT: Record<SiLabel, string> = {
  permissive: "permissive", middle: "middle", tougher: "tougher", insufficient: "insufficient", unavailable: "n/a", bye: "BYE",
};

function one(v: string | string[] | undefined): string | undefined {
  return Array.isArray(v) ? v[0] : v;
}

function fmt(m: SiMetric) {
  if (m.value === null) return null;
  return m.metric.endsWith("_per_play") ? `${m.value >= 0 ? "+" : ""}${m.value.toFixed(3)}` : `${(m.value * 100).toFixed(1)}%`;
}

function num(m: SiMetric) {
  if (m.numerator === null) return "—";
  return m.metric.endsWith("_per_play") ? m.numerator.toFixed(2) : String(m.numerator);
}

function primary(w: SiWeek): SiMetric | undefined {
  return w.evidence?.metrics.find((m) => m.metric === w.evidence?.primary_metric);
}

function Chip({ w, showWeek = true }: { w: SiWeek; showWeek?: boolean }) {
  const where = w.status === "scheduled" ? `${w.home ? "vs" : "@"} ${w.opponent}` : w.status === "bye" ? "BYE" : "?";
  const p = primary(w);
  const title = w.evidence?.sentence ?? w.reason ?? w.label_text;
  return (
    <span title={`Week ${w.week}: ${title}`} style={{ display: "inline-block", marginRight: 4, marginBottom: 2 }}>
      {showWeek && <span className="faint" style={{ fontSize: 11 }}>{w.week} </span>}
      <Badge value={w.status === "scheduled" ? `${where}${p?.rank ? ` · ${p.rank}` : ""}` : where} tone={TONE[w.label]} />
    </span>
  );
}

function Counts({ c }: { c: Record<SiLabel, number> }) {
  const parts = (["permissive", "middle", "tougher", "insufficient", "unavailable", "bye"] as SiLabel[])
    .filter((k) => c[k] > 0).map((k) => `${c[k]} ${SHORT[k]}`);
  return <span className="faint">{parts.length ? parts.join(" · ") : "—"}</span>;
}

export default async function ScheduleIntelligencePage({ searchParams }: PageProps<"/schedule-intelligence">) {
  const sp = await searchParams;
  const q = new URLSearchParams();
  const team = one(sp.team), position = one(sp.position) ?? "all", phase = one(sp.phase) ?? "all";
  const selected = one(sp.player);
  if (team) q.set("team_id", team);
  q.set("position", position);
  q.set("phase", phase);
  const res = await api<ScheduleIntelligence>(`/api/schedule-intelligence?${q.toString()}`);
  let detail: SiPlayer | undefined;
  if (res.ok && selected) {
    detail = res.data.players.find((p) => String(p.espn_player_id) === selected);
    if (!detail) {
      const single = await api<ScheduleIntelligence>(`/api/schedule-intelligence?player_id=${selected}&phase=${phase}`);
      if (single.ok) detail = single.data.players[0];
    }
  }
  return (
    <div className="stack">
      <div className="page-head">
        <h1>Schedule intelligence</h1>
        <span className="sub">Upcoming opponents and what each defense has <b>observed</b> allowing · descriptive, not a projection</span>
      </div>
      {!res.ok ? <ErrorBox error={res.error} /> : <Body d={res.data} detail={detail} position={position} phase={phase} />}
    </div>
  );
}

function Body({ d, detail, position, phase }: { d: ScheduleIntelligence; detail?: SiPlayer; position: string; phase: string }) {
  const w = d.window;
  const href = (pid?: number) => {
    const p = new URLSearchParams();
    if (d.query.team_id !== null) p.set("team", String(d.query.team_id));
    p.set("position", position);
    p.set("phase", phase);
    if (pid !== undefined) p.set("player", String(pid));
    return `/schedule-intelligence?${p.toString()}`;
  };
  return (
    <>
      <p className="faint" style={{ margin: 0 }}>
        Weeks {w.weeks[0] ?? "—"}–{w.weeks[w.weeks.length - 1] ?? "—"}
        {w.regular_season_through !== null && <> · regular season through week {w.regular_season_through} · fantasy playoffs {w.playoff_weeks.length ? `weeks ${w.playoff_weeks.join(", ")}` : "unknown"} (from league settings)</>}
        {" "}· first actionable week {w.first_actionable_week ?? "none"}
      </p>
      <Freshness d={d} />
      {!w.remaining_weeks && <Callout>No remaining fantasy scoring periods: every week through the final period has kicked off. Past weeks are shown for information only.</Callout>}
      {w.uncertain && w.warning && <Callout>{w.warning}</Callout>}
      {!d.schedule.available && <Callout>No NFL schedule is loaded, so no opponents can be assigned. Run the NFL sync.</Callout>}
      {d.schedule.available && d.schedule.warnings.map((x) => (
        UNKNOWN_INPUT.has(x.code) ? <Callout key={x.code}>{x.message}</Callout>
          : <p key={x.code} className="faint" style={{ margin: 0 }}>{x.message}</p>))}

      <form action="/schedule-intelligence" className="panel">
        <div className="row-flex">
          <label>Team <select name="team" defaultValue={d.query.team_id ?? ""}>
            {d.teams.map((t) => <option key={t.team_id} value={t.team_id}>{t.name}{t.is_mine ? " (you)" : ""}</option>)}
          </select></label>
          <label>Position <select name="position" defaultValue={position}>
            {["all", "QB", "RB", "WR", "TE"].map((p) => <option key={p} value={p}>{p === "all" ? "QB/RB/WR/TE" : p}</option>)}
          </select></label>
          <label>Weeks <select name="phase" defaultValue={phase}>
            <option value="all">All remaining</option><option value="regular">Regular season</option><option value="playoffs">Fantasy playoffs</option>
          </select></label>
          <label>Player <select name="player" defaultValue={detail ? String(detail.espn_player_id) : ""}>
            <option value="">—</option>
            {d.players.map((p) => <option key={p.espn_player_id} value={p.espn_player_id}>{p.name} ({p.position})</option>)}
          </select></label>
          <button type="submit">Show</button>
          <Link href="/schedule-intelligence">reset</Link>
        </div>
      </form>

      <Legend d={d} />

      {d.players.length === 0
        ? <Callout>No {position === "all" ? "QB/RB/WR/TE" : position} players on this roster.</Callout>
        : <Overview d={d} href={href} />}

      {detail && <Detail p={detail} />}
      {phase !== "regular" && d.players.length > 0 && w.playoff_weeks.length > 0 && <Playoffs d={d} href={href} />}

      <details className="panel method">
        <summary>What this is (and is not)</summary>
        <ul className="reasons">{d.notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
        <details>
          <summary>How games are presumed complete (and what that cannot rule out)</summary>
          <p>{d.schedule.completion_rule}</p>
          <ul className="reasons">{d.schedule.completion_limits.map((n, i) => <li key={i}>{n}</li>)}</ul>
        </details>
        <details>
          <summary>Metric definitions</summary>
          <ul className="reasons">
            {Object.entries(d.metrics).map(([k, m]) => (
              <li key={k}><b>{m.label}</b> ({m.positions.join(", ")}): {m.definition} <span className="faint">[{m.numerator} / {m.denominator}]</span></li>
            ))}
          </ul>
        </details>
      </details>
    </>
  );
}

// Warnings that mean no defensive evidence can be shown at all.
const UNKNOWN_INPUT = new Set(["schedule_refresh_unknown", "defense_refresh_unknown"]);

// When each input was last refreshed by a successful sync for this season. A sync that refreshed
// only other NFL tables is listed separately and is not evidence that these inputs are current.
function Freshness({ d }: { d: ScheduleIntelligence }) {
  const f = d.freshness;
  const latest = f.latest_nfl_sync;
  const skipped = latest ? [!latest.refreshed_nfl_schedules && "schedule", !latest.refreshed_nfl_defense_week && "defensive aggregates"].filter(Boolean) : [];
  return (
    <p className="faint" style={{ margin: 0 }}>
      NFL schedule refreshed {f.nfl_schedules ? when(f.nfl_schedules.refreshed_at) : <b>never (unknown)</b>}
      {" "}· defensive aggregates refreshed {f.nfl_defense_week ? when(f.nfl_defense_week.refreshed_at) : <b>never (unknown)</b>}
      {f.defense_weeks_loaded.length > 0 && <> (weeks {Math.min(...f.defense_weeks_loaded)}–{Math.max(...f.defense_weeks_loaded)} loaded)</>}
      {latest && skipped.length > 0 && <> · latest {f.season} NFL sync {when(latest.refreshed_at)} did not refresh the {skipped.join(" or ")}</>}
      {d.schedule.completion_reference_time && <> · games presumed complete if kicked off ≥ 8 h before {when(d.schedule.completion_reference_time)}</>}
    </p>
  );
}

function Legend({ d }: { d: ScheduleIntelligence }) {
  return (
    <div className="row-flex">
      {(["permissive", "middle", "tougher", "insufficient", "bye"] as SiLabel[]).map((k) => (
        <Badge key={k} value={d.labels[k]} tone={TONE[k]} />
      ))}
      <span className="faint" title={d.label_rule}>
        Chip number = rank of the opponent on the position&apos;s primary metric (1 = most permissive). Hover for the evidence. <u>Rule</u>
      </span>
    </div>
  );
}

function Overview({ d, href }: { d: ScheduleIntelligence; href: (pid?: number) => string }) {
  return (
    <section className="panel">
      <h2>{d.team?.name ?? "Players"}: upcoming matchups</h2>
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>Player</th><th>Next actionable week</th><th>Opponent evidence (primary metric)</th>
              <th>Regular season</th><th>Fantasy playoffs</th><th>Coverage</th><th /></tr>
          </thead>
          <tbody>
            {d.players.map((p) => {
              const next = p.weeks.find((w) => w.actionable);
              const pm = next ? primary(next) : undefined;
              const reg = p.weeks.filter((w) => w.actionable && w.phase === "regular");
              const po = p.weeks.filter((w) => w.actionable && w.phase === "playoffs");
              const gaps = p.summary.all.counts.insufficient + p.summary.all.counts.unavailable;
              return (
                <tr key={p.espn_player_id}>
                  <td><PlayerLink id={p.espn_player_id} name={p.name} /> <Pos value={p.position} /> <span className="faint">{p.pro_team ?? "no team"}</span>
                    {p.on_ir && <> <Badge value="IR" tone="bad" /></>}
                    {p.notes.length > 0 && <span className="faint" title={p.notes.join("\n")}> ⓘ</span>}</td>
                  <td>{next ? <Chip w={next} /> : <span className="faint">none</span>}</td>
                  <td className="wrap" style={{ maxWidth: 360 }}>
                    {next?.evidence ? <>{pm && fmt(pm) !== null
                      ? <><b>{fmt(pm)}</b> <span className="faint">{pm.label} · {pm.denominator} plays · {next.evidence.games} games{pm.rank ? ` · rank ${pm.rank}/${pm.ranked_defenses}` : ""}</span></>
                      : <span className="faint">{pm?.missing_reason ?? "unavailable"}</span>}</>
                      : <span className="faint">{next?.reason ?? "—"}</span>}
                  </td>
                  <td className="wrap">{reg.length ? reg.map((w) => <Chip key={w.week} w={w} />) : <span className="faint">—</span>}</td>
                  <td className="wrap">{po.length ? po.map((w) => <Chip key={w.week} w={w} />) : <span className="faint">—</span>}</td>
                  <td>{gaps > 0 ? <Badge value={`${gaps} wk without evidence`} tone="warn" /> : <span className="faint">all weeks</span>}</td>
                  <td><Link href={href(p.espn_player_id)}>details</Link></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Playoffs({ d, href }: { d: ScheduleIntelligence; href: (pid?: number) => string }) {
  const weeks = d.window.playoff_weeks;
  return (
    <section className="panel">
      <h2>Fantasy playoffs · weeks {weeks.join(", ")}</h2>
      <p className="faint">Playoff weeks from the league&apos;s settings. Labels describe each opponent&apos;s observed defense; they do not add or subtract fantasy points.</p>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Player</th>{weeks.map((w) => <th key={w}>Week {w}{d.window.matchup_periods[String(w)] ? <span className="faint"> · matchup {d.window.matchup_periods[String(w)]}</span> : null}</th>)}<th>Summary</th></tr></thead>
          <tbody>
            {d.players.map((p) => (
              <tr key={p.espn_player_id}>
                <td><Link href={href(p.espn_player_id)}>{p.name}</Link> <Pos value={p.position} /></td>
                {weeks.map((wk) => {
                  const w = p.weeks.find((x) => x.week === wk);
                  return <td key={wk}>{w ? <Chip w={w} showWeek={false} /> : <span className="faint">—</span>}</td>;
                })}
                <td><Counts c={p.summary.playoffs.counts} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Detail({ p }: { p: SiPlayer }) {
  return (
    <section className="panel">
      <h2>{p.name} ({p.position}, {p.pro_team ?? "no team"}): week by week</h2>
      {p.notes.map((n, i) => <Callout key={i}>{n}</Callout>)}
      <div className="row-flex" style={{ marginBottom: 8 }}>
        {(["next", "regular", "playoffs", "all"] as const).map((k) => (
          <span key={k}><b>{k === "all" ? "All remaining" : k === "next" ? "Next" : k === "regular" ? "Regular season" : "Playoffs"}:</b> <Counts c={p.summary[k].counts} /></span>
        ))}
      </div>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Wk</th><th>Phase</th><th>Opponent</th><th>Observed defense</th>
            <th>Metrics (value · numerator / denominator · rank)</th><th>Sample</th></tr></thead>
          <tbody>
            {p.weeks.map((w) => (
              <tr key={w.week} style={w.actionable ? undefined : { opacity: 0.6 }}>
                <td>{w.week}{!w.actionable && <div className="faint" style={{ fontSize: 11 }}>{w.window_note}</div>}</td>
                <td className="faint">{w.phase ?? "?"}{w.matchup_period ? ` · m${w.matchup_period}` : ""}</td>
                <td>{w.status === "scheduled" ? `${w.home ? "vs" : "@"} ${w.opponent}` : w.status === "bye" ? "BYE" : <span className="faint">{w.reason}</span>}</td>
                <td className="wrap" style={{ maxWidth: 420 }}><Badge value={w.label_text} tone={TONE[w.label]} />{" "}
                  <span className="faint">{w.evidence?.sentence ?? w.reason}</span></td>
                <td className="wrap">{w.evidence ? w.evidence.metrics.map((m) => (
                  <div key={m.metric}>
                    {m.label}: <b>{fmt(m) ?? "—"}</b>
                    {m.value !== null && <span className="faint"> · {num(m)} / {m.denominator}{m.rank ? ` · rank ${m.rank}/${m.ranked_defenses}` : ""}</span>}
                    {m.position_coverage !== undefined && m.position_coverage !== null && <span className="faint"> · positions identified for {(m.position_coverage * 100).toFixed(0)}% of targets</span>}
                    {m.missing_reason && <span className="faint"> · {m.missing_reason}</span>}
                  </div>
                )) : <span className="faint">—</span>}</td>
                <td className="faint">{w.evidence ? <>{w.evidence.games} games{w.evidence.through_week !== null ? `, through wk ${w.evidence.through_week}` : ""}
                  {w.evidence.excluded_weeks.length > 0 && (
                    <div title={Object.entries(w.evidence.excluded_reasons).map(([wk, why]) => `wk ${wk}: ${why.reason}`).join("\n")}>
                      excluded (not presumed complete): wk {w.evidence.excluded_weeks.join(", ")}</div>)}</> : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
