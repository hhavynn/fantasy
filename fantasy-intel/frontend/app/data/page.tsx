import { PlayerLink } from "@/components/PlayerLink";
import { Badge, ErrorBox, Pos } from "@/components/ui";
import { api } from "@/lib/api";
import { num, when } from "@/lib/format";

type MapRow = {
  espn_player_id: number; espn_name: string; espn_position: string; espn_team: string | null; gsis_id: string | null;
  nfl_name: string | null; nfl_team: string | null; match_method: string; match_confidence: string; notes: string | null;
  availability_status?: string | null; percent_owned?: number | null;
};
type Issue = { severity: string; check_name: string; detail: string; count: number; examples: unknown[] };
type ExtImport = {
  import_id: string; provider: string | null; dataset: string | null; source_filename: string; season: number | null;
  weeks: number[] | null; imported_at: string; source_updated_at: string | null; row_count: number; matched_rows: number;
  unmatched_rows: number; ambiguous_rows: number; warning_count: number; error_count: number; status: string;
  duplicate_of: string | null; message: string | null;
};
type External = {
  imports: ExtImport[];
  coverage: { provider: string; dataset: string; season: number; weeks: number[]; rows: number; matched: number;
    unmatched: number; ambiguous: number; updated_at: string | null }[];
  unmatched: { provider: string; dataset: string; season: number; week: number; player_name: string | null;
    team: string | null; position: string | null; provider_player_id: string | null; source_player_key: string;
    mapping_method: string; mapping_reason: string | null }[];
  issues: { import_id: string; severity: string; check_name: string; detail: string; week: number | null;
    row_number: number | null; player_name: string | null }[];
  selection: Record<string, { provider: string | null; status: string; providers: string[]; message: string }> | null;
  duplicate_noops?: number; rejected?: number;
};
type ReturnedCov = { returned: number; with_projection: number; without_projection: number; zero: number };
type WeekCov = {
  week: number; espn_total: number | null; returned: number; returned_with_projection: number;
  returned_without_projection: number; zero: number; truncated: boolean; complete: boolean; reason: string | null;
  rostered: ReturnedCov & { rostered: number; expected: number; expected_returned: number;
    expected_with_projection: number; omitted: number };
  available: ReturnedCov;
};
type Projections = {
  window: { current: number; final: number; last_regular: number | null } | null;
  latest_fetch: { run_id: string; finished_at: string | null;
    fetch: { weeks_requested: number[]; weeks_complete: number[]; weeks_unverified: number[]; weeks_failed: number[];
      failures: Record<string, string>; coverage: WeekCov[]; projection_rows: number; history_rows: number;
      current_week: number; final_scoring_period: number | null; source: string };
    ros: { players?: number; full_coverage?: number; incomplete?: number; rostered_incomplete?: number; skipped?: string } | null } | null;
  stored: { week: number; projected: number; zero: number; rostered_projected: number; rostered: number; latest_synced_at: string | null }[];
  ros_summary: { players: number; full_coverage: number; incomplete: number; computed_at: string | null } | null;
  ros_incomplete: { espn_player_id: number; full_name: string | null; position: string | null; pro_team: string | null;
    availability_status: string | null; ros_weeks_expected: number; ros_weeks_available: number; missing_weeks: number[] }[];
  history: { rows: number; days: number; latest: string | null } | null;
  note: string;
};
type DQ = {
  validation_issues: Issue[]; schema_version: number | null; external: External; espn_projections: Projections | null;
  mapping_summary: { match_method: string; match_confidence: string; n: number }[];
  unmatched: MapRow[]; medium_confidence: MapRow[]; conflicts: MapRow[];
  sync_runs: { run_id: string; source: string; started_at: string; finished_at: string | null; status: string; message: string | null }[];
};

export default async function DataQualityPage() {
  const res = await api<DQ>("/api/data-quality");
  if (!res.ok) return <ErrorBox error={res.error} />;
  const d = res.data;
  return (
    <div className="stack">
      <div className="page-head">
        <h1>Data quality</h1>
        <span className="sub">ESPN ↔ nflverse ID mapping and sync history. Unmatched players are listed here, never silently dropped.</span>
      </div>
      <div className="grid grid-2">
        <section className="panel">
          <h2>ID mapping summary</h2>
          <table>
            <thead><tr><th>Method</th><th>Confidence</th><th className="num">Players</th></tr></thead>
            <tbody>
              {d.mapping_summary.map((m) => (
                <tr key={m.match_method + m.match_confidence}><td className="mono">{m.match_method}</td><td>{m.match_confidence}</td><td className="num">{m.n}</td></tr>
              ))}
            </tbody>
          </table>
        </section>
        <section className="panel">
          <h2>Sync runs</h2>
          <table>
            <thead><tr><th>Source</th><th>Started</th><th>Finished</th><th>Status</th><th>Message</th></tr></thead>
            <tbody>
              {d.sync_runs.map((r) => (
                <tr key={r.run_id}><td>{r.source}</td><td>{when(r.started_at)}</td><td>{when(r.finished_at)}</td>
                  <td><Badge value={r.status.toUpperCase()} tone={r.status === "ok" ? "good" : "bad"} /></td><td className="faint wrap">{r.message}</td></tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>
      <section className="panel">
        <h2>Validation (latest analytics run) · schema v{d.schema_version ?? "?"}</h2>
        {d.validation_issues.length === 0 ? <p className="faint">No validation issues.</p> : (
          <table>
            <thead><tr><th>Severity</th><th>Check</th><th>Detail</th><th className="num">Count</th><th>Examples</th></tr></thead>
            <tbody>
              {d.validation_issues.map((i) => (
                <tr key={i.check_name}>
                  <td><Badge value={i.severity.toUpperCase()} tone={i.severity === "error" ? "bad" : "warn"} /></td>
                  <td className="mono">{i.check_name}</td><td className="wrap">{i.detail}</td>
                  <td className="num">{i.count}</td><td className="wrap faint mono">{JSON.stringify(i.examples).slice(0, 160)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="faint" style={{ marginTop: 8 }}>Errors (logical corruption) abort the analytics run; warnings are statistically odd but kept.</p>
      </section>
      {d.espn_projections && <ProjectionSection p={d.espn_projections} />}
      <ExternalSection x={d.external} />
      <MapTable title="Unmatched players (rostered first, then by ownership)" rows={d.unmatched} />
      <MapTable title="Name-matched players (medium confidence: verify)" rows={d.medium_confidence} />
      {d.conflicts.length > 0 && <MapTable title="Conflicting crosswalks" rows={d.conflicts} />}
    </div>
  );
}

function MapTable({ title, rows }: { title: string; rows: MapRow[] }) {
  return (
    <section className="panel">
      <h2>{title} · {rows.length}</h2>
      {rows.length === 0 ? <p className="faint">None.</p> : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>ESPN player</th><th>ESPN id</th><th>Team</th><th>Status</th><th className="num">% own</th><th>nflverse</th><th>Notes</th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.espn_player_id}>
                  <td><PlayerLink id={r.espn_player_id} name={r.espn_name} /> <Pos value={r.espn_position} /></td>
                  <td className="mono">{r.espn_player_id}</td><td>{r.espn_team}</td>
                  <td className="faint">{r.availability_status ?? ""}</td><td className="num">{num(r.percent_owned, 1)}</td>
                  <td>{r.gsis_id ? <span className="mono">{r.gsis_id}</span> : ""} {r.nfl_name} {r.nfl_team}</td>
                  <td className="wrap faint">{r.notes}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}


function ExternalSection({ x }: { x: External }) {
  const last = x.imports[0];
  return (
    <section className="panel">
      <h2>External utilization (routes / situational usage)</h2>
      {x.imports.length === 0 ? (
        <p className="faint">
          No external utilization imported. Routes stay <b>unavailable</b> in-season. Import an authorized normalized CSV with{" "}
          <code>uv run python ../scripts/import_external_usage.py FILE --provider NAME --dataset NAME</code>.
        </p>
      ) : (
        <div className="stack">
          <div className="row-flex faint">
            <span>Last import: <b>{when(last.imported_at)}</b> · {last.provider} / {last.dataset} ·{" "}
              <Badge value={last.status.toUpperCase()} tone={last.status === "imported" ? "good" : last.status === "rejected" ? "bad" : "warn"} /></span>
            <span>· {x.duplicate_noops ?? 0} duplicate/no-op · {x.rejected ?? 0} rejected</span>
          </div>
          {x.selection && Object.entries(x.selection).map(([season, sel]) => (
            <div key={season} className={sel.status === "conflict_unresolved" ? "callout" : "faint"}>
              {season}: provider selection <b>{sel.status}</b>: {sel.message}
            </div>
          ))}
          <table>
            <thead><tr><th>Provider</th><th>Dataset</th><th>Season</th><th>Weeks covered</th><th className="num">Rows</th>
              <th className="num">Matched</th><th className="num">Unmatched</th><th className="num">Ambiguous</th><th>Source updated</th></tr></thead>
            <tbody>
              {x.coverage.map((c) => (
                <tr key={`${c.provider}-${c.dataset}-${c.season}`}>
                  <td>{c.provider}</td><td>{c.dataset}</td><td>{c.season}</td><td className="mono">{c.weeks.join(", ")}</td>
                  <td className="num">{c.rows}</td><td className="num">{c.matched}</td><td className="num">{c.unmatched}</td>
                  <td className="num">{c.ambiguous}</td><td>{when(c.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <details>
            <summary>Import history ({x.imports.length})</summary>
            <table>
              <thead><tr><th>Imported</th><th>File</th><th>Provider / dataset</th><th>Status</th><th className="num">Rows</th>
                <th className="num">Matched</th><th className="num">Unm.</th><th className="num">Amb.</th><th className="num">Warn</th>
                <th className="num">Err</th><th>Message</th></tr></thead>
              <tbody>
                {x.imports.map((i) => (
                  <tr key={i.import_id}>
                    <td>{when(i.imported_at)}</td><td className="mono">{i.source_filename}</td><td>{i.provider} / {i.dataset}</td>
                    <td><Badge value={i.status.toUpperCase()} tone={i.status === "imported" ? "good" : i.status === "rejected" ? "bad" : "warn"} /></td>
                    <td className="num">{i.row_count}</td><td className="num">{i.matched_rows}</td><td className="num">{i.unmatched_rows}</td>
                    <td className="num">{i.ambiguous_rows}</td><td className="num">{i.warning_count}</td><td className="num">{i.error_count}</td>
                    <td className="wrap faint">{i.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
          <details open={x.issues.some((i) => i.severity === "error")}>
            <summary>Validation issues ({x.issues.length})</summary>
            <table>
              <thead><tr><th>Severity</th><th>Check</th><th>Row</th><th>Wk</th><th>Player</th><th>Detail</th></tr></thead>
              <tbody>
                {x.issues.map((i, n) => (
                  <tr key={`${i.import_id}-${n}`}>
                    <td><Badge value={i.severity.toUpperCase()} tone={i.severity === "error" ? "bad" : "warn"} /></td>
                    <td className="mono">{i.check_name}</td><td>{i.row_number ?? ""}</td><td>{i.week ?? ""}</td>
                    <td>{i.player_name ?? ""}</td><td className="wrap faint">{i.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
          <div>
            <h3>Unmatched / ambiguous external players · {x.unmatched.length}</h3>
            {x.unmatched.length === 0 ? <p className="faint">None.</p> : (
              <table>
                <thead><tr><th>Source name</th><th>Team</th><th>Pos</th><th>Provider ID</th><th>Source key</th><th>Season/Wk</th>
                  <th>Status</th><th>Reason</th></tr></thead>
                <tbody>
                  {x.unmatched.map((u) => (
                    <tr key={`${u.provider}-${u.dataset}-${u.season}-${u.week}-${u.source_player_key}`}>
                      <td>{u.player_name ?? "—"}</td><td>{u.team ?? ""}</td><td>{u.position ?? ""}</td>
                      <td className="mono">{u.provider_player_id ?? ""}</td><td className="mono">{u.source_player_key}</td>
                      <td>{u.season} / {u.week}</td><td><Badge value={u.mapping_method.toUpperCase()} tone="warn" /></td>
                      <td className="wrap faint">{u.mapping_reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <p className="faint" style={{ marginTop: 6 }}>
              Resolve with <code>uv run python ../scripts/map_external_player.py --provider P --source-player-key KEY --gsis-id ID</code>.
            </p>
          </div>
        </div>
      )}
    </section>
  );
}


function ProjectionSection({ p }: { p: Projections }) {
  const f = p.latest_fetch?.fetch;
  const failed = f?.weeks_failed ?? [];
  const unverified = f?.weeks_unverified ?? [];
  const stored = new Map(p.stored.map((s) => [s.week, s]));
  return (
    <section className="panel">
      <h2>ESPN future projections</h2>
      {!f ? (
        <p className="faint">No ESPN sync has fetched forward projections yet. Run <code>uv run python ../scripts/sync_espn.py</code>.</p>
      ) : (
        <div className="stack">
          <div className="row-flex faint">
            <span>Latest sync: <b>{when(p.latest_fetch?.finished_at)}</b></span>
            <span>· weeks requested {f.weeks_requested.length ? `${f.weeks_requested[0]}–${f.weeks_requested[f.weeks_requested.length - 1]}` : "none"}
              {" "}(league current {f.current_week}, final {f.final_scoring_period ?? "?"}
              {p.window?.last_regular ? `, regular season ends wk ${p.window.last_regular}` : ""})</span>
            <span>· {f.projection_rows} projection rows · history {p.history?.rows ?? 0} rows over {p.history?.days ?? 0} day(s)</span>
          </div>
          {failed.length > 0 && (
            <div className="callout">
              <b>Failed week requests: {failed.join(", ")}.</b> Previously stored projections for these weeks were kept
              (stale is safer than treating a network failure as missing).
              <ul className="reasons">{Object.entries(f.failures).map(([w, m]) => <li key={w}>{m}</li>)}</ul>
            </div>
          )}
          {unverified.length > 0 && (
            <div className="callout">
              <b>Weeks {unverified.join(", ")}: universe not verified.</b> Only players ESPN explicitly returned were
              updated; omitted players keep their previous projection (absence alone isn&apos;t evidence).
              <ul className="reasons">{f.coverage.filter((c) => !c.complete).map((c) => <li key={c.week}>wk {c.week}: {c.reason}</li>)}</ul>
            </div>
          )}
          <div className="table-wrap">
            <table>
              <thead><tr><th>Week</th><th>Fetch</th>
                <th className="num" title="X-Fantasy-Filter-Player-Count: ESPN's total of ONTEAM + FREEAGENT + WAIVERS players">ESPN total</th>
                <th className="num">Returned</th><th className="num">With proj</th><th className="num">No proj</th>
                <th className="num">Zero</th>
                <th className="num" title="rostered players with an NFL team that have a projection / rostered players with an NFL team">Rostered</th>
                <th className="num" title="returned free agents / waivers with a projection / returned (no expected FA count exists beyond ESPN's total)">FA / waivers</th>
                <th className="num" title="projections stored for this week now (rostered with projection / rostered)">Stored now</th></tr></thead>
              <tbody>
                {f.weeks_requested.map((w) => {
                  const c = f.coverage.find((x) => x.week === w);
                  const s = stored.get(w);
                  const status = failed.includes(w) ? "FAILED" : unverified.includes(w) ? "UNVERIFIED" : "COMPLETE";
                  return (
                    <tr key={w}>
                      <td>{w}{p.window?.last_regular && w > p.window.last_regular ? <span className="faint"> (playoffs)</span> : ""}</td>
                      <td><Badge value={status} tone={status === "COMPLETE" ? "good" : status === "FAILED" ? "bad" : "warn"}
                        title={c?.reason ?? undefined} /></td>
                      <td className="num">{c ? (c.espn_total ?? <span className="missing" title="Header missing">—</span>) : "—"}</td>
                      <td className="num">{c ? c.returned : "—"}</td>
                      <td className="num">{c ? c.returned_with_projection : "—"}</td>
                      <td className="num">{c ? c.returned_without_projection : "—"}</td>
                      <td className="num">{c ? c.zero : "—"}</td>
                      <td className="num">{c ? <>{c.rostered.expected_with_projection}/{c.rostered.expected}
                        {c.rostered.rostered !== c.rostered.expected && <span className="faint" title="rostered players without a current NFL team are not expected">
                          {" "}(+{c.rostered.rostered - c.rostered.expected} no NFL team)</span>}</> : "—"}</td>
                      <td className="num">{c ? `${c.available.with_projection}/${c.available.returned}` : "—"}</td>
                      <td className="num">{s ? <>{s.projected} <span className="faint">({s.rostered_projected}/{s.rostered})</span></> : "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <p className="faint">
            Complete = the response matched ESPN&apos;s own player count, wasn&apos;t truncated and included every rostered
            player with an NFL team; only then can players ESPN omitted lose a stored projection. Zero = ESPN projected 0
            (a bye, injury or role call: byes are labeled from the NFL schedule, not from zeros). No proj = ESPN returned
            the player without a projection: unknown, never treated as 0.
          </p>
          {p.ros_summary && (
            <div>
              <h3>Rest-of-season totals · {p.ros_summary.full_coverage}/{p.ros_summary.players} players with full coverage</h3>
              {p.ros_incomplete.length === 0 ? <p className="faint">Every player has a projection for every remaining week.</p> : (
                <details>
                  <summary>{p.ros_summary.incomplete} players with incomplete coverage (ROS total withheld; rostered first)</summary>
                  <table>
                    <thead><tr><th>Player</th><th>Team</th><th>Status</th><th className="num">Weeks</th><th>Missing</th></tr></thead>
                    <tbody>
                      {p.ros_incomplete.map((r) => (
                        <tr key={r.espn_player_id}>
                          <td><PlayerLink id={r.espn_player_id} name={r.full_name ?? String(r.espn_player_id)} /> <Pos value={r.position} /></td>
                          <td>{r.pro_team ?? "FA"}</td><td className="faint">{r.availability_status ?? ""}</td>
                          <td className="num">{r.ros_weeks_available}/{r.ros_weeks_expected}</td>
                          <td className="mono faint">{r.missing_weeks.join(", ")}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </details>
              )}
            </div>
          )}
          <p className="faint">{p.note} Source: ESPN. Not used in any score or recommendation.</p>
        </div>
      )}
    </section>
  );
}
