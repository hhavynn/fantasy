import type { Matchup, TeamSide } from "./matchup-types.ts";
import { headline } from "./matchup.ts";

/** Export only explicitly approved numeric data; never emit team/player names, IDs, league title or URLs. */
export function matchupCopyText(m: Pick<Matchup, "week" | "league" | "status" | "me" | "opponent" | "provenance">, projectionBy: string): string {
  const state = m.status === "in_progress" ? "In progress at last sync" : m.status === "final" ? "Final" :
    m.status === "upcoming" ? "Not started" : "Status unknown";
  const leagueSignal = m.provenance?.contract === "signal-provenance.v1"
    ? m.provenance.signals.find((s) => s.signal === "league") : undefined;
  const rawTime = leagueSignal?.captured_at;
  const time = rawTime && Number.isFinite(Date.parse(rawTime)) ? new Date(rawTime).toISOString() : "unknown";
  const stale = Boolean(leagueSignal?.stale_reason);
  const side = (value: TeamSide | null): string => {
    if (!value) return "No opponent scheduled";
    const h = headline(value, m.status);
    const n = h.value === null || !Number.isFinite(h.value) ? "—" : h.value.toFixed(1);
    return `${n} ${h.basis === "actual" ? "actual points" : h.basis === "projected" ? "projected points" :
      h.basis === "known" ? "known projected points only (incomplete)" : "points unavailable"}`;
  };
  return [
    `Fantasy Intel · Week ${m.week} · ${m.league.season}`,
    `Matchup state: ${state}`,
    `My side: ${side(m.me)}`,
    `Opponent: ${side(m.opponent)}`,
    `Projection basis: ${projectionBy}`,
    `League snapshot captured: ${time}`,
    ...(stale ? ["Warning: source data has been superseded"] : []),
    "Cached source data, not a live score. No outcome or win probability is implied.",
  ].join("\n");
}
