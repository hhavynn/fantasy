// Trade counterparty scouting (issue #215): GET /api/managers/{team_id}/counterparty. Descriptive patterns in one
// team's recorded, executed trades, each with its numerator, denominator and date window. The UI only formats what
// the backend counted: no likelihoods, no personality labels.

export type CounterpartyWindow = {
  seasons: number[]; first_trade_at: string | null; last_trade_at: string | null; through: string | null; text: string;
};

export type CounterpartyRow = {
  label: string; numerator: number; denominator: number; share: number | null;
  players?: number; players_denominator?: number;
  team_id?: number; season?: number | null; name?: string | null; matched_to_current_team?: boolean; recurring?: boolean;
  league_trades?: number | null;
};

export type CounterpartyPattern = {
  key: string; title: string; denominator: number; denominator_label: string;
  suppressed: boolean; suppressed_reason: string | null; rows: CounterpartyRow[];
  unknown: number; unknown_label: string | null; window: CounterpartyWindow;
};

export type CounterpartyLeg = {
  espn_player_id: number; name: string | null; position: string | null; from_team_id: number | null; to_team_id: number | null;
};

export type CounterpartyTrade = {
  event_id: string; season: number; week: number | null; processed_at: string | null; proposed_at: string | null;
  shape: string | null; legs_recorded: boolean; unlisted_assets: string[];
  partners: { team_id: number; season: number | null; name: string | null; matched_to_current_team: boolean }[];
  players_sent: CounterpartyLeg[]; players_received: CounterpartyLeg[];
};

export type CounterpartySeason = {
  season: number; status: "included" | "excluded" | "not_recorded";
  team_trades: number | null; league_trades: number | null; reason: string | null;
};

export type CounterpartyReport = {
  team: { team_id: number; name: string; owner_names: string | null; is_mine: boolean };
  status: "recorded" | "not_recorded";
  window: CounterpartyWindow;
  min_trades: number;
  suppression_rule: string;
  summary: {
    trades: number | null; league_trades: number | null; trades_with_player_legs: number | null;
    trades_without_player_legs: number | null; multi_team_trades: number | null;
    trades_with_unlisted_assets: number | null; players_sent: number | null; players_received: number | null;
    recurring_partners: number | null;
    /** League trades whose stored records name at most one team, not this one: this team may be their unrecorded
     *  side, so while this is above 0 the team's count is a minimum. */
    league_trades_unattributed: number | null;
  };
  patterns: CounterpartyPattern[];
  seasons: CounterpartySeason[];
  trades: CounterpartyTrade[];
  trades_listed: number;
  discovery: { opponent_team_id: number; positions_sent: string[] };
  notes: string[];
};

/** Positions trade discovery's targeted lens accepts (`pos`). */
export const DISCOVERY_POSITIONS = ["QB", "RB", "WR", "TE", "K", "D/ST"] as const;

/** Trade discovery with one manager as the opponent, optionally targeting a position you would receive. The page's
 *  own URL keys (`opponent`, `mode`, `pos`); render it through TeamLink, which keeps the league and analysed team. */
export function discoveryHref(opponent: number, position?: string): string {
  const p = new URLSearchParams({ mode: "targeted", opponent: String(opponent) });
  if (position) p.set("pos", position);
  return `/trades/discover?${p.toString()}`;
}

export type DiscoveryLink = { href: string; position: string | null; basis: string };

/**
 * Discovery links for one manager: every trade with them, then each position with a stated basis, in position order.
 * A position comes from the counterparty report (sent in recorded trades, only when that pattern is not suppressed:
 * the backend leaves the list empty below the threshold) or from the current roster (more than the league average).
 * None when this manager is the analysed team (`useTeam`), or, with no team analysed, the league's default team:
 * trade discovery never offers a trade with your own team.
 */
export function discoveryLinks(report: Pick<CounterpartyReport, "team" | "discovery">,
  roster: { position: string; league_comparison: string }[] = [], analysedTeam: number | null = null): DiscoveryLink[] {
  const opp = report.discovery.opponent_team_id;
  if (analysedTeam !== null ? analysedTeam === opp : report.team.is_mine) return [];
  const sent = new Set(report.discovery.positions_sent);
  const above = new Set(roster.filter((r) => r.league_comparison === "ABOVE").map((r) => r.position));
  const out: DiscoveryLink[] = [{ href: discoveryHref(opp), position: null, basis: "All positions" }];
  for (const pos of DISCOVERY_POSITIONS) {
    const why = [sent.has(pos) && "sent in recorded trades", above.has(pos) && "rostered above the league average now"]
      .filter(Boolean).join("; ");
    if (why) out.push({ href: discoveryHref(opp, pos), position: pos, basis: why });
  }
  return out;
}

/** "3 of 4", the counted trades against their denominator. */
export const ofText = (n: number, d: number) => `${n} of ${d}`;

/** Seasons that were left out or unknown, for the disclosure under the patterns. */
export function seasonDisclosures(seasons: CounterpartySeason[]): CounterpartySeason[] {
  return seasons.filter((s) => s.status !== "included");
}
