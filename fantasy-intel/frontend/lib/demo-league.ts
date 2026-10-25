// The demo league on /leagues/demo (issue #167): recorded engine output for the synthetic fixture league, written by
// scripts/build_demo_league.py into lib/fixtures/demo-league.synthetic.json. Bundled with the site, so the demo needs
// no backend, database, account or storage, and can never show a real league. Pure so node --test covers it.

export const DEMO_LABEL = "Demo league · synthetic data";

export type DemoPlayer = { name: string; position: string; proTeam: string | null; projection: number | null };
export type DemoLeague = {
  league: { name: string; season: number; week: number; teams: number; receptionPoints: number | null };
  team: string;
  lineup: { status: string; projectedGain: number | null; currentTotal: number | null; recommendedTotal: number | null;
    start: DemoPlayer[]; bench: DemoPlayer[] };
  standings: { team: string; wins: number; losses: number; ties: number; pointsFor: number; pointsAgainst: number;
    seed: number | null; mine: boolean }[];
  waivers: { name: string; position: string; proTeam: string | null; recommendation: string | null;
    waiverScore: number | null; reason: string | null }[];
};

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const text = (v: unknown): string | null => (typeof v === "string" && v.trim() ? v : null);
const list = (v: unknown): Record<string, unknown>[] => (Array.isArray(v) ? v.filter(isRecord) : []);

function player(p: Record<string, unknown>): DemoPlayer | null {
  const name = text(p.name);
  const position = text(p.position);
  return name && position ? { name, position, proTeam: text(p.pro_team), projection: num(p.projection) } : null;
}

/** The fixture as the page uses it, or null when it is not the expected shape (the demo entry is then hidden). */
export function parseDemoLeague(raw: unknown): DemoLeague | null {
  if (!isRecord(raw) || !isRecord(raw.league) || !isRecord(raw.lineup)) return null;
  const { league, lineup } = raw;
  const name = text(league.name);
  const season = num(league.season);
  const week = num(league.week);
  const team = text(raw.team);
  if (!name || season === null || week === null || !team) return null;
  return {
    league: { name, season, week, teams: num(league.teams) ?? 0, receptionPoints: num(league.reception_points) },
    team,
    lineup: {
      status: text(lineup.status) ?? "unknown",
      projectedGain: num(lineup.projected_gain), currentTotal: num(lineup.current_total), recommendedTotal: num(lineup.recommended_total),
      start: list(lineup.start).map(player).filter((p): p is DemoPlayer => p !== null),
      bench: list(lineup.bench).map(player).filter((p): p is DemoPlayer => p !== null),
    },
    standings: list(raw.standings).flatMap((t) => {
      const teamName = text(t.team);
      const [w, l, ti, pf, pa] = [num(t.wins), num(t.losses), num(t.ties), num(t.points_for), num(t.points_against)];
      return teamName && w !== null && l !== null && ti !== null && pf !== null && pa !== null
        ? [{ team: teamName, wins: w, losses: l, ties: ti, pointsFor: pf, pointsAgainst: pa, seed: num(t.seed), mine: t.mine === true }]
        : [];
    }),
    waivers: list(raw.waivers).flatMap((p) => {
      const n = text(p.name);
      const pos = text(p.position);
      return n && pos ? [{ name: n, position: pos, proTeam: text(p.pro_team), recommendation: text(p.recommendation),
        waiverScore: num(p.waiver_score), reason: text(p.reason) }] : [];
    }),
  };
}

/** "W-L" or "W-L-T". */
export function record(t: { wins: number; losses: number; ties: number }): string {
  return t.ties ? `${t.wins}-${t.losses}-${t.ties}` : `${t.wins}-${t.losses}`;
}
