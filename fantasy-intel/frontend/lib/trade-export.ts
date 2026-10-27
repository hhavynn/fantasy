import type { TradeResultV3 } from "./trade-types.ts";

/** An explicitly name-free local clipboard summary, never a public share URL or raw result payload. */
export function tradeCopyText(
  r: Pick<TradeResultV3, "league" | "trade" | "evaluation" | "team_a" | "team_b" | "provenance">,
  projectionLabel: string,
): string {
  const pts = (value: number | null | undefined): string =>
    value === null || value === undefined || !Number.isFinite(value) ? "—" :
    `${value > 0 && Number(value.toFixed(1)) !== 0 ? "+" : ""}${value.toFixed(1)}`;
  const signal = r.provenance?.contract === "signal-provenance.v1"
    ? r.provenance.signals.find((s) => s.signal === "league") : undefined;
  const raw = signal?.captured_at;
  const captured = raw && Number.isFinite(Date.parse(raw)) ? new Date(raw).toISOString() : "unknown";
  const weeks = r.evaluation.weeks ?? [];
  const incomplete = r.team_a.deltas.ros_delta === null || r.team_b.deltas.ros_delta === null;
  const partial = Boolean(r.team_a.deltas.unsupported_slots?.length || r.team_b.deltas.unsupported_slots?.length);
  return [
    `Fantasy Intel · hypothetical trade · ${r.league.season}`,
    `Evaluation weeks: ${weeks.length ? `${weeks[0]}–${weeks[weeks.length - 1]}` : "unknown"}`,
    `Side A sends: ${r.trade.team_a_sends.length} ${r.trade.team_a_sends.length === 1 ? "player" : "players"}`,
    `Side B sends: ${r.trade.team_b_sends.length} ${r.trade.team_b_sends.length === 1 ? "player" : "players"}`,
    `Side A projected rest-of-season lineup change: ${pts(r.team_a.deltas.ros_delta)} points`,
    `Side B projected rest-of-season lineup change: ${pts(r.team_b.deltas.ros_delta)} points`,
    `Projection source: ${projectionLabel}`,
    `League snapshot captured: ${captured}`,
    ...(signal?.stale_reason ? ["Warning: the league source was superseded by a later sync attempt"] : []),
    ...(incomplete ? ["At least one projected total is incomplete or unknown; do not infer a winner"] : []),
    ...(partial ? ["Unsupported lineup slots may make full-roster comparisons partial"] : []),
    "Hypothetical read-only lineup simulation, not an offer, acceptance likelihood or provider transaction.",
  ].join("\n");
}
