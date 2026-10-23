// Pure helpers for the opportunity tree on /injury-impact (issue #190): the response types, labels and the links to
// waiver eligibility. No React and no `@/` imports, so they run under the unit-test runner. The contract is
// docs/opportunity_tree.md and backend/app/analytics/opportunity_tree.py.
import type { InjuryLevel, Roster, UsageSide } from "./injury-impact.ts";
import { plural } from "./plural.ts";

export type Claim = "yours" | "opponent" | "rostered" | "waivers" | "free_agent" | "available_claim_unknown" | "unknown";
export type Ownership = { claim: Claim; claimable: boolean | null; needs_claim: boolean | null };
export type Availability = { level: InjuryLevel | "AVAILABLE" | "TAXI" | "UNKNOWN"; known: boolean; sources: string[] };

export type TreePerson = {
  gsis_id: string;
  espn_player_id: number | null;
  full_name: string;
  position: string | null;
  depth_rank: number | null;
  availability: Availability;
  roster: Roster;
  ownership: Ownership;
};

export type Shares = {
  team_games: number;
  games_played: number;
  weeks: number[];
  snap_share: number | null;
  route_share: number | null;
  route_games: number;
  touch_share: number | null;
  touches_per_game: number | null;
};

export type Personnel = { kind: "first_game" | "missed" | "returned" | "reserve_list"; week: number; games: number | null; roster_status?: string };

export type Peer = TreePerson & { starter_in_scenario: boolean; shares: Shares; personnel: Personnel[] };

export type Effect = TreePerson & {
  basis: "observed" | "hypothesis";
  reasons: ("observed_gain" | "gained_while_out" | "next_on_depth_chart" | "pro_rata_estimate")[];
  observed: { with: UsageSide | null; without: UsageSide | null; delta_per_game: number | null };
  estimate: { share: number | null; extra_per_game: number | null } | null;
};

export type Certainty = "designated_out" | "if_he_sits" | "if_he_plays" | "status_cleared" | "unconfirmed";
export type Outcome = "supported" | "hypothesis" | "no_supported_beneficiary" | "abstain";

export type Evidence =
  | { source: "status"; level: InjuryLevel; sources: string[]; nfl_report_status: string | null; practice_status: string | null }
  | { source: "nfl_report"; week: number; report_status: string | null; practice_status: string | null }
  | { source: "provider_status"; provider: string; status: string };

export type Scenario = {
  kind: "teammate_unavailable" | "teammate_returning";
  subject: TreePerson & { absence: { kind?: string; last_played_week: number; consecutive_team_games_missed: number } };
  certainty: Certainty;
  evidence: Evidence[];
  unit: "touches" | "targets" | "pass_attempts";
  unit_label: string;
  sample: { team_games_with: number; team_games_without: number; weeks_without: number[] };
  outcome: Outcome;
  abstain: { code: string; text: string } | null;
  effects: Effect[];
};

export type TreeTeam = {
  team: string;
  game: { week: number; opponent: string | null; is_home: boolean | null; kickoff: string | null; started: boolean; on_bye: boolean | null };
  injury_freshness: { status: string; reason: string } | null;
  depth_chart: { known: boolean; as_of: string | null };
  coverage: { status: "complete" | "gap" | "unverified"; missing_weeks: number[]; reason: string | null };
  groups: { position: string; ordered_by: "depth_chart" | "snap_share"; peers: Peer[]; more: number }[];
  scenarios: Scenario[];
  affects_my_roster: boolean;
};

export type OpportunityTreeResponse = {
  version: string;
  season: number;
  week: number;
  generated_at: string;
  league_context: {
    available: boolean; league_name: string | null; my_team_id: number | null; perspective: "query" | "owner_default";
    provider: "espn" | "sleeper"; waiver_status_known: boolean;
  };
  sources: {
    usage_through_week: number | null; depth_chart_as_of: string | null; schedule_synced: boolean;
    weekly_rosters_synced: boolean; route_providers: string[];
  };
  method: { observed: string; hypothesis: string; abstain: string; shares: string; thresholds: Record<string, unknown> };
  counts: { teams: number; unavailable: number; returning: number; supported: number; hypothesis: number; no_supported_beneficiary: number; abstained: number };
  notices: { tone: "info" | "caution" | "negative"; code: string; text: string }[];
  teams: TreeTeam[];
};

// ----------------------------------------------------------------------------- labels

export const SCENARIO_LABEL: Record<Scenario["kind"], string> = {
  teammate_unavailable: "Teammate unavailable",
  teammate_returning: "Teammate returning",
};

/** How settled the change is, in words. Never "confirmed": a designation or a cleared status is not a game-day fact. */
export const CERTAINTY_LABEL: Record<Certainty, string> = {
  designated_out: "Designated out",
  if_he_sits: "If he sits",
  if_he_plays: "If he plays",
  status_cleared: "Status cleared, not confirmed",
  unconfirmed: "Return unconfirmed",
};

export const OUTCOME_LABEL: Record<Outcome, { text: string; tone: "good" | "info" | "warn" | "" }> = {
  supported: { text: "Observed", tone: "good" },
  hypothesis: { text: "Hypothesis only", tone: "warn" },
  no_supported_beneficiary: { text: "No supported beneficiary", tone: "" },
  abstain: { text: "Abstains", tone: "" },
};

export const REASON_LABEL: Record<Effect["reasons"][number], string> = {
  observed_gain: "Gained in games without him",
  gained_while_out: "Gained while he was out; that share may go back",
  next_on_depth_chart: "Next on the depth chart",
  pro_rata_estimate: "Pro-rata split of his volume",
};

const UNIT_SHORT: Record<Scenario["unit"], string> = { touches: "touches", targets: "targets", pass_attempts: "att" };

export function unitShort(unit: Scenario["unit"]): string {
  return UNIT_SHORT[unit];
}

/** "4 games with him, 2 without (weeks 5, 6)". */
export function sampleText(s: Scenario): string {
  const { team_games_with: w, team_games_without: wo, weeks_without: weeks } = s.sample;
  const wk = weeks.length ? ` (${plural(weeks.length, "week")} ${weeks.join(", ")})` : "";
  return `${w} ${plural(w, "game")} with him, ${wo} without${wk}`;
}

/** One line per piece of evidence behind the scenario's status. */
export function evidenceText(e: Evidence): string {
  if (e.source === "status") {
    const src = e.sources.length ? e.sources.join(" and ") : "status";
    return [src, e.practice_status].filter(Boolean).join(" · ");
  }
  if (e.source === "nfl_report") {
    return [`Week ${e.week} NFL report: ${e.report_status ?? "no game designation"}`, e.practice_status].filter(Boolean).join(" · ");
  }
  return `${e.provider} status: ${e.status.toLowerCase().replaceAll("_", " ")}`;
}

/** A personnel change as a factual sentence with its week. */
export function personnelText(p: Personnel, team: string): string {
  switch (p.kind) {
    case "first_game": return `First game for ${team} this season in week ${p.week}`;
    case "missed": return `Missed the last ${p.games === 1 ? "game" : `${p.games} games`} (since week ${p.week})`;
    case "returned": return `Back in week ${p.week} after missing ${p.games === 1 ? "1 game" : `${p.games} games`}`;
    case "reserve_list": return `Reserve list (${p.roster_status ?? "reserve"}) on the week ${p.week} roster`;
  }
}

/**
 * Who can add him in this league, in words. A player on another manager's roster is never claimable; where the
 * provider does not tell waivers from free agents (Sleeper) a free agent is never "no claim needed".
 */
export function claimText(o: Ownership, roster: Roster, provider: string): { text: string; tone: "info" | "good" | "warn" | ""; detail: string } {
  switch (o.claim) {
    case "yours": return { text: "Your team", tone: "info", detail: "On the selected roster" };
    case "opponent": return { text: roster.fantasy_team ?? "Rostered", tone: "", detail: "On another manager's roster: not claimable" };
    case "rostered": return { text: "Rostered", tone: "", detail: "On a roster in this league: not claimable" };
    case "waivers": return { text: "Waivers", tone: "warn", detail: "Needs a waiver claim" };
    case "free_agent": return { text: "Free agent", tone: "good", detail: `Can be added in ${provider} without a claim` };
    case "available_claim_unknown": return { text: "Unrostered", tone: "warn", detail: `${provider} does not report waivers: whether adding him needs a claim is unknown` };
    default: return { text: "Unknown", tone: "", detail: "Not in this league's synced player pool" };
  }
}

/** The add/drop check on the waivers page for a claimable player (it checks eligibility, claim rules and drops). */
export function eligibilityHref(p: TreePerson): string | null {
  if (p.ownership.claimable !== true || p.espn_player_id === null) return null;
  const pos = p.position && ["QB", "RB", "WR", "TE"].includes(p.position) ? `pos=${p.position}&` : "";
  return `/waivers?${pos}add=${p.espn_player_id}#add-drop`;
}

/** "62%" or null (the caller renders a muted dash with a reason). */
export function sharePct(v: number | null): string | null {
  return v === null ? null : `${Math.round(v * 100)}%`;
}

/** The team row's summary: "1 unavailable · 1 returning", then what the data supports. */
export function teamSummary(t: TreeTeam): { scenarios: string; outcome: string } {
  const un = t.scenarios.filter((s) => s.kind === "teammate_unavailable").length;
  const ret = t.scenarios.length - un;
  const parts = [un ? `${un} unavailable` : null, ret ? `${ret} returning` : null].filter(Boolean);
  const observed = t.scenarios.reduce((n, s) => n + s.effects.filter((e) => e.basis === "observed").length, 0);
  const hyp = t.scenarios.reduce((n, s) => n + s.effects.filter((e) => e.basis === "hypothesis").length, 0);
  const abst = t.scenarios.filter((s) => s.outcome === "abstain").length;
  const out = [observed ? `${observed} observed` : null, hyp ? `${hyp} ${plural(hyp, "hypothesis", "hypotheses")}` : null,
    abst ? `${abst} ${plural(abst, "abstention")}` : null].filter(Boolean);
  return { scenarios: parts.join(" · "), outcome: out.length ? out.join(" · ") : "No supported beneficiary" };
}
