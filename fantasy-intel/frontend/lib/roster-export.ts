import type { TeamValue, TvPhase } from "./types.ts";

const count = (n: number) => n.toLocaleString("en-US");
/** Missing projections remain unknown; a measured zero is always distinct from an absent value. */
function pts(value: number | null | undefined): string {
  return value === null || value === undefined || !Number.isFinite(value) ? "—" : value.toFixed(1);
}
function capturedAt(raw: string | null | undefined): string {
  if (!raw || !Number.isFinite(Date.parse(raw))) return "unknown";
  return new Date(raw).toISOString();
}

/**
 * Allowlist-only summary of an already authorized, league-scoped Team Value response.
 * No owner, player or team names, IDs, invite URLs or raw provider tokens are serialized.
 */
export function rosterCopyText(d: Pick<TeamValue, "league" | "window" | "freshness" | "overview" | "players">, phase: TvPhase): string {
  const split = d.overview?.phases?.[phase];
  const players = d.players;
  const weeks = split?.weeks ?? [];
  const active = players?.filter((p) => !p.on_ir && !p.on_taxi).length;
  const ir = players?.filter((p) => p.on_ir).length;
  const taxi = players?.filter((p) => p.on_taxi).length;
  const complete = split && split.incomplete_weeks.length === 0 && split.projected_points !== null;
  const source = d.freshness.league_synced_at;
  const projected = split?.projected_points ?? null;
  const known = split?.known_points ?? null;
  return [
    `Fantasy Intel · name-free roster outlook · ${d.league.season}`,
    `Perspective: selected authorized roster (identities withheld)`,
    `Window: ${phase === "all" ? "Rest of season" : phase === "regular" ? "Regular season" : "Playoffs"}`,
    `Weeks evaluated: ${weeks.length ? weeks.join(", ") : "none available"}`,
    `Rostered players: ${players ? count(players.length) : "unknown"}`,
    `Active roster players: ${active === undefined ? "unknown" : count(active)}`,
    `IR: ${ir === undefined ? "unknown" : count(ir)} · Taxi: ${taxi === undefined ? "unknown" : count(taxi)}`,
    `Projected starting-lineup points: ${complete ? pts(projected) : "—"}`,
    `Known-only starting-lineup points: ${pts(known)}${complete ? " (complete)" : " (partial or unavailable)"}`,
    `Incomplete projection weeks: ${split ? (split.incomplete_weeks.length ? split.incomplete_weeks.join(", ") : "none") : "unknown"}`,
    `League snapshot captured: ${capturedAt(source)}`,
    `Projection snapshot captured: ${capturedAt(d.freshness.projections_synced_at)}`,
    "Read-only league-scoped projections, not actual results or a guaranteed future lineup.",
  ].join("\n");
}
