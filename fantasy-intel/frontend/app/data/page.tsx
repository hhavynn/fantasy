import { PlayerLink } from "@/components/PlayerLink";
import { Badge, ErrorBox, Pos } from "@/components/ui";
import { api } from "@/lib/api";
import { num, when } from "@/lib/format";

type MapRow = {
  espn_player_id: number; espn_name: string; espn_position: string; espn_team: string | null; gsis_id: string | null;
  nfl_name: string | null; nfl_team: string | null; match_method: string; match_confidence: string; notes: string | null;
  availability_status?: string | null; percent_owned?: number | null;
};
type DQ = {
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
