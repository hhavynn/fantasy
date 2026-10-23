// FAAB bid guidance (issue #179): the capability audit of the league's synced FAAB data, the league's observed winning
// bids, and a labelled heuristic bid range per claim of the device-local claim plan. The backend
// (GET /api/waivers/faab?claims=ADD:DROP,...&share=N, contract waiver-faab/v1, through the /api/waiver-faab route) builds
// every number; this module only builds the scoped URL and the words. Nothing here bids or talks to ESPN or Sleeper.

import type { Claim } from "./claim-plan.ts";
import { MAX_CLAIMS } from "./claim-plan.ts";
import { leaguePath } from "./league-scope.ts";
import { plural } from "./plural.ts";
import type { ProvenancePayload } from "./signal-provenance.ts";

export const SHARE_CHOICES = [10, 25, 50, 100] as const;
export const DEFAULT_SHARE = 25;

export function isShare(n: unknown): n is number {
  return typeof n === "number" && Number.isInteger(n) && n >= 1 && n <= 100;
}

/** `ADD[:DROP]` per claim: the guidance never depends on a bid, so typing one does not refetch it. */
export function faabClaimParam(c: Pick<Claim, "add" | "drop">): string {
  return c.drop === null ? String(c.add) : `${c.add}:${c.drop}`;
}

/**
 * The browser URL for the guidance on `claims` for `team` in `leagueId`: always under /l/<lg>/, so the proxy scopes the
 * request to that league. The plan and the share are both in the query, so both are part of every cache key.
 */
export function faabSrc(leagueId: string, team: number, claims: readonly Pick<Claim, "add" | "drop">[], share: number): string {
  const q = new URLSearchParams({
    team_id: String(team),
    claims: claims.slice(0, MAX_CLAIMS).map(faabClaimParam).join(","),
    share: String(isShare(share) ? share : DEFAULT_SHARE),
  });
  return `${leaguePath(leagueId, "/api/waiver-faab")}?${q}`;
}

const CLAIM = /^-?\d{1,13}(:-?\d{1,13})?$/;

/** The backend path for the browser's query: only `team_id`, a well-formed `claims` list and `share` are forwarded. */
export function faabBackendPath(params: URLSearchParams): string | null {
  const team = params.get("team_id");
  if (team === null || !/^\d{1,6}$/.test(team)) return null;
  const share = params.get("share") ?? String(DEFAULT_SHARE);
  if (!/^\d{1,3}$/.test(share) || !isShare(Number(share))) return null;
  const raw = params.get("claims") ?? "";
  const claims = raw === "" ? [] : raw.split(",");
  if (claims.length > MAX_CLAIMS || claims.some((c) => !CLAIM.test(c))) return null;
  return `/api/waivers/faab?${new URLSearchParams({ team_id: team, claims: claims.join(","), share })}`;
}

// ---- the backend contract -----------------------------------------------------------------------------------------

export type AuditStatus = "supplied" | "missing" | "not_supplied" | "not_applicable";
export type AuditField = {
  field: string; label: string; status: AuditStatus; value: unknown; display: string | null;
  /** The provider field, only when it supplied a value. */
  source: string | null; reason: string | null;
};
export type Capability = {
  flag: "handled" | "partial" | "unsupported";
  guidance: "available" | "unavailable" | "not_applicable";
  observed_bids: "available" | "none_yet" | "unavailable" | "not_applicable";
  reasons: string[];
  fields: AuditField[];
};
export type BidStats = { count: number; median: number; min: number; max: number; median_share_pct: number | null };
export type Urgency = "high" | "medium" | "low" | "none";
export type GuidanceStatus =
  | "range" | "no_bid_needed" | "no_gain" | "value_unknown" | "not_evaluated" | "blocked" | "budget_exhausted" | "unavailable";
export type BidRange = { low: number; high: number; cap: number; cap_share_pct: number; band_pct: [number, number] };
export type ClaimGuidance = {
  rank: number;
  add: { espn_player_id: number; name: string | null; position: string | null };
  drop: number | null;
  claim_kind: "waiver_claim" | "free_agent" | null;
  validation: "legal" | "unverified" | "invalid";
  status: GuidanceStatus;
  urgency: Urgency | null;
  range: BidRange | null;
  /** The range scaled so every claim could win within the budget; null when that would fall below the minimum bid. */
  if_all_win?: { low: number; high: number } | null;
  fundable_if_all_win?: boolean;
  reason: string | null;
  notes: { code: string; message: string }[];
  basis: { ros_gain: number | null; known_ros_gain: number | null; weeks: number; gain_per_week: number | null; share_pct: number };
  observed: (BidStats & { position: string; relation?: "below" | "around" | "above" | null }) | null;
};
export type FaabResponse = {
  contract: "waiver-faab/v1";
  submitted: false;
  status: "empty" | "evaluated";
  team: { id: number | null; name: string | null };
  capability: Capability;
  inputs: { share_pct: number; share_choices: number[]; default_share_pct: number; high_per_week: number;
    medium_per_week: number; bands_pct: Record<"high" | "medium" | "low", [number, number]>; min_observed: number };
  budget: {
    acquisition: "FAAB" | "PRIORITY" | "UNKNOWN"; budget: number | null; spent: number | null; remaining: number | null;
    minimum_bid: number | null; weeks_left: number; even_split_per_week: number | null;
    other_teams: { teams: number; known: number; max_remaining: number | null };
  };
  observed: {
    claims: number; with_bid: number; without_bid: number; overall: BidStats | null;
    by_position: Record<string, BidStats>;
    recent: { week: number | null; espn_player_id: number | null; name: string | null; position: string | null; bid: number }[];
  } | null;
  claims: ClaimGuidance[];
  combined: {
    ranged_claims: number; high_total: number | null; max_spend_if_won: number | null; remaining: number | null;
    fits: boolean | null; complete: boolean; unranged_ranks: number[];
    exclusive_groups: { kind: "shared_drop" | "open_spots"; ranks: number[]; capacity: number }[]; scaled: boolean;
    /** The most ranged claims that can win together; whether they could all be funded at a known minimum bid. */
    max_winners: number; fundable_at_minimum: boolean | null; unfundable_ranks: number[];
  } | null;
  assumptions: string[];
  limitations: string[];
  provenance?: ProvenancePayload | null;
  /** Added by the /api/waiver-faab route: the canonical league the answer was read from. */
  scope?: string;
  /** Added by the route: the backend served this league's previous files, so the budget may be older. */
  stale?: boolean;
};

// ---- display rules ------------------------------------------------------------------------------------------------

export const money = (n: number) => `$${Number.isInteger(n) ? n : n.toFixed(2)}`;

export const URGENCY_LABEL: Record<Urgency, string> = {
  high: "High urgency", medium: "Medium urgency", low: "Low urgency", none: "No urgency",
};

export function rangeText(r: Pick<BidRange, "low" | "high">): string {
  return r.low === r.high ? money(r.low) : `${money(r.low)}–${money(r.high)}`;
}

/** Where an entered bid sits against the heuristic range; null when either is missing. */
export function bidVsRange(bid: number | null, r: Pick<BidRange, "low" | "high"> | null): "below" | "within" | "above" | null {
  if (bid === null || r === null) return null;
  return bid < r.low ? "below" : bid > r.high ? "above" : "within";
}

/** The one-line budget summary, from synced values only; unknown stays unknown. */
export function budgetText(b: FaabResponse["budget"]): string {
  if (b.acquisition !== "FAAB") return "";
  const left = b.remaining !== null && b.budget !== null
    ? `${money(b.remaining)} of ${money(b.budget)} FAAB left (synced)`
    : "Remaining FAAB unknown: the budget or your spend is not synced";
  const parts = [left];
  if (b.even_split_per_week !== null && b.weeks_left > 0) parts.push(`even split ${money(b.even_split_per_week)} a week over ${b.weeks_left} weeks`);
  parts.push(b.minimum_bid !== null ? `minimum bid ${money(b.minimum_bid)}` : "minimum bid not synced");
  return parts.join(" · ");
}

/** The observed-bid line for one position, or why there is none. */
export function observedText(o: ClaimGuidance["observed"], minObserved: number): string | null {
  if (!o) return null;
  const head = `Observed: ${o.count} winning ${o.position} ${plural(o.count, "bid")} in this league this season, median ${money(o.median)}, highest ${money(o.max)}`;
  if (o.count < minObserved) return `${head}. Too few to compare.`;
  const rel = o.relation === "below" ? "below" : o.relation === "above" ? "above" : "around";
  return `${head}. This range is ${rel} the median.`;
}

/** Sentence-case label for an audit status. */
export const AUDIT_LABEL: Record<AuditStatus, string> = {
  supplied: "Synced", missing: "Not in the synced data", not_supplied: "Not supplied by the provider",
  not_applicable: "Not used in this league",
};
