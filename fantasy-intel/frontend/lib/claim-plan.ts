// Waiver claim planner (issue #180): the device-local shortlist in claim order, each claim with an optional drop and,
// where the league bids with FAAB, an optional bid. The plan lives only in this browser, under the canonical league
// ID and the team it is for; the backend (GET /api/waivers/claim-plan?claims=..., contract waiver-claim-plan/v1, through the
// /api/waiver-claim-plan route) only checks it. Nothing here submits a claim or talks to ESPN or Sleeper.

import { isLeagueId, leaguePath } from "./league-scope.ts";
import { provenanceStatus, signalsOf, type ProvenancePayload, type ProvenanceStatus } from "./signal-provenance.ts";

/** The backend checks at most this many claims (app/analytics/claim_plan.py MAX_CLAIMS). */
export const MAX_CLAIMS = 12;
/** Whole currency units; the backend refuses larger bids. */
export const MAX_BID = 100_000;

export type Claim = { add: number; drop: number | null; bid: number | null };
type Stored = { v: 1; league: string; team: number; claims: Claim[] };

const validId = (id: unknown): id is number => typeof id === "number" && Number.isSafeInteger(id) && id !== 0 && Math.abs(id) <= 1e12;
const validBid = (b: unknown): b is number => typeof b === "number" && Number.isSafeInteger(b) && b >= 0 && b <= MAX_BID;
const validTeam = (t: unknown): t is number => typeof t === "number" && Number.isSafeInteger(t) && t >= 0 && t <= 999_999;

/** No shared fallback: a plan needs a canonical league ID and a known team, or it is not stored at all. */
export function claimPlanKey(leagueId: string | null, team: number | null): string | null {
  return isLeagueId(leagueId) && validTeam(team) ? `fi:waiver-claim-plan:v1:${leagueId}:${team}` : null;
}

/**
 * The stored claims, or none. The payload repeats its league and team, so a value copied under another key (another
 * league, another team) is refused rather than carried over. Malformed, duplicate or self-dropping claims are refused.
 */
export function parseClaimPlan(raw: string | null, leagueId: string, team: number): Claim[] {
  try {
    const v = JSON.parse(raw ?? "null") as Partial<Stored> | null;
    if (!v || v.v !== 1 || v.league !== leagueId || v.team !== team || !Array.isArray(v.claims)) return [];
    const seen = new Set<number>();
    const out: Claim[] = [];
    for (const c of v.claims as unknown[]) {
      if (!c || typeof c !== "object") return [];
      const { add, drop, bid } = c as Record<string, unknown>;
      if (!validId(add) || seen.has(add)) return [];
      if (drop !== null && (!validId(drop) || drop === add)) return [];
      if (bid !== null && !validBid(bid)) return [];
      seen.add(add);
      out.push({ add, drop: drop as number | null, bid: bid as number | null });
    }
    return out;
  } catch {
    return [];
  }
}

export function serializeClaimPlan(leagueId: string, team: number, claims: Claim[]): string {
  return JSON.stringify({ v: 1, league: leagueId, team, claims } satisfies Stored);
}

export function readClaimPlan(storage: Pick<Storage, "getItem">, leagueId: string | null, team: number | null): { claims: Claim[]; available: boolean } {
  const key = claimPlanKey(leagueId, team);
  if (!key) return { claims: [], available: false };
  try {
    return { claims: parseClaimPlan(storage.getItem(key), leagueId!, team!), available: true };
  } catch {
    return { claims: [], available: false };
  }
}

export function writeClaimPlan(storage: Pick<Storage, "setItem">, leagueId: string | null, team: number | null, claims: Claim[]): boolean {
  const key = claimPlanKey(leagueId, team);
  if (!key) return false;
  try {
    storage.setItem(key, serializeClaimPlan(leagueId!, team!, claims));
    return true;
  } catch {
    return false;
  }
}

/**
 * The plan for the current shortlist: saved claims for players still shortlisted, in their claim order, then newly
 * shortlisted players at the end with no drop or bid. A player removed from the shortlist leaves the plan.
 */
export function reconcile(claims: readonly Claim[], shortlist: readonly number[]): Claim[] {
  const wanted = new Set(shortlist);
  const kept = claims.filter((c) => wanted.has(c.add));
  const have = new Set(kept.map((c) => c.add));
  return [...kept, ...shortlist.filter((id) => !have.has(id)).map((add) => ({ add, drop: null, bid: null }))];
}

/** Move the claim at `from` to `to` (both clamped), without changing the input. */
export function moveClaim(claims: readonly Claim[], from: number, to: number): Claim[] {
  const out = [...claims];
  if (from < 0 || from >= out.length) return out;
  const target = Math.max(0, Math.min(out.length - 1, to));
  const [c] = out.splice(from, 1);
  out.splice(target, 0, c);
  return out;
}

export function updateClaim(claims: readonly Claim[], add: number, patch: Partial<Omit<Claim, "add">>): Claim[] {
  return claims.map((c) => {
    if (c.add !== add) return c;
    const next = { ...c, ...patch };
    if (next.drop !== null && (!validId(next.drop) || next.drop === add)) next.drop = null;
    if (next.bid !== null && !validBid(next.bid)) next.bid = null;
    return next;
  });
}

/** A bid field's text as a bid: empty is none; anything but a whole number in range is refused (null, invalid). */
export function parseBid(text: string): { bid: number | null; ok: boolean } {
  const t = text.trim();
  if (t === "") return { bid: null, ok: true };
  if (!/^\d{1,6}$/.test(t)) return { bid: null, ok: false };
  const n = Number(t);
  return validBid(n) ? { bid: n, ok: true } : { bid: null, ok: false };
}

/** `ADD[:DROP[:BID]]` per claim, as the backend reads it. */
export function claimParam(c: Claim): string {
  return `${c.add}:${c.drop ?? ""}:${c.bid ?? ""}`;
}

/**
 * The browser URL that checks `claims` for `team` in `leagueId`: always under /l/<lg>/, so the proxy scopes the
 * request to that league; there is no unscoped form. Only the first MAX_CLAIMS claims are sent.
 */
export function claimPlanSrc(leagueId: string, team: number, claims: readonly Claim[]): string {
  const q = new URLSearchParams({ team_id: String(team), claims: claims.slice(0, MAX_CLAIMS).map(claimParam).join(",") });
  return `${leaguePath(leagueId, "/api/waiver-claim-plan")}?${q}`;
}

const CLAIM = /^-?\d{1,13}(:(-?\d{1,13})?)?(:(\d{1,6})?)?$/;

/**
 * The backend path for the browser's query: only `team_id` and a well-formed `claims` list are forwarded. The plan is
 * one ordered parameter (the league-scoped response cache keys on sorted query pairs, so repeated parameters in a
 * new order would read the answer for the old one).
 */
export function claimPlanBackendPath(params: URLSearchParams): string | null {
  const team = params.get("team_id");
  if (team === null || !/^\d{1,6}$/.test(team)) return null;
  const raw = params.get("claims") ?? "";
  const claims = raw === "" ? [] : raw.split(",");
  if (claims.length > MAX_CLAIMS || claims.some((c) => !CLAIM.test(c))) return null;
  return `/api/waivers/claim-plan?${new URLSearchParams({ team_id: team, claims: claims.join(",") })}`;
}

// ---- the backend contract -----------------------------------------------------------------------------------------

export type Validation = "legal" | "unverified" | "invalid";
export type ClaimReason = { code: string; message: string; effect: Validation | "info"; player_id: number | null };
export type PlanPlayer = {
  espn_player_id: number; name: string | null; position: string | null; pro_team: string | null;
  injury_status?: string | null; roster_status: string; availability_status?: string | null;
  team_id?: number | null; team_name?: string | null; on_ir?: boolean; on_taxi?: boolean;
};
export type DropOption = {
  espn_player_id: number; name: string | null; position: string | null; pro_team: string | null;
  injury_status: string | null; on_ir?: boolean; on_taxi?: boolean; known_projected_points: number | null;
};
export type Fact = { value: unknown; display: string | null; reason: string | null };
export type PlanClaim = {
  rank: number;
  request: Claim;
  validation: Validation;
  reasons: ClaimReason[];
  add: PlanPlayer | null;
  drop: PlanPlayer | null;
  claim_kind: "waiver_claim" | "free_agent" | null;
  simulation_status: "evaluated" | "blocked" | "options" | null;
  roster: { active_after: number | null; open_spots_after: number | null; legal: boolean | null; drop_required: boolean | null };
  suggested_drop: ({ available: true; espn_player_id: number; name: string | null; lineup_cost: number | null; known_lineup_cost: number | null }
    | { available: false; reason: string }) | null;
  impact: {
    first_week: { week: number; delta: number | null; known_delta: number; projection_complete: boolean } | null;
    rest_of_season: { ros_delta: number | null; known_ros_delta: number; weeks_incomplete: number[]; projection_complete: boolean };
  } | null;
};
export type PlanRules = {
  acquisition: "FAAB" | "PRIORITY" | "UNKNOWN";
  is_faab: boolean | null; faab_budget: number | null; faab_spent: number | null; faab_remaining: number | null;
  waiver_priority: number | null;
  waiver_type: Fact | null; waiver_period: Fact | null; process_days: Fact | null;
  unknown: { rule: string; reason: string }[];
};
export type ClaimPlanResponse = {
  contract: "waiver-claim-plan/v1";
  submitted: false;
  status: "empty" | "evaluated";
  max_claims: number;
  team: { id: number | null; name: string | null };
  rules: PlanRules;
  roster: { active_capacity: number; active_before: number | null; open_spots_before: number | null; drop_options: DropOption[] };
  claims: PlanClaim[];
  summary: { claims: number; legal: number; unverified: number; invalid: number; total_bid: number | null;
    remaining_after_bids: number | null;
    conflicts: { kind: string; player_id: number | null; ranks: number[]; position?: string; limit?: number; open_spots?: number }[] };
  league?: { name?: string | null } | null;
  provenance?: ProvenancePayload | null;
  /** Added by the /api/waiver-claim-plan route: the canonical league the answer was read from. */
  scope?: string;
  /** Added by the route: the backend served this league's previous files (X-FI-Degraded), so the data may be older. */
  stale?: boolean;
};

// ---- display rules ------------------------------------------------------------------------------------------------

export const VALIDATION_LABEL: Record<Validation, string> = {
  legal: "Checks pass", unverified: "Needs review", invalid: "Not valid",
};

/**
 * The league data's freshness is derived when the page shows it (league-scoped answers are cached per data version),
 * so a claim the backend found legal is only shown as passing while the league signal is current.
 */
export function leagueFreshness(p: ProvenancePayload | null | undefined, now: number = Date.now()): ProvenanceStatus {
  const league = signalsOf(p).find((s) => s.signal === "league");
  return league ? provenanceStatus(league, now) : "unknown";
}

/** The freshness a plan's checks are shown with: stale whenever the answer came from older league files. */
export function planFreshness(res: Pick<ClaimPlanResponse, "provenance" | "stale">, now: number = Date.now()): ProvenanceStatus {
  return res.stale === true ? "stale" : leagueFreshness(res.provenance, now);
}

const FRESHNESS_REASON: Partial<Record<ProvenanceStatus, string>> = {
  stale: "The league data is stale: rosters and availability may have changed since the last sync.",
  unknown: "The age of the league data is unknown, so availability cannot be confirmed.",
  unavailable: "The league data is missing, so availability cannot be confirmed.",
  partial: "The league data is incomplete, so availability cannot be confirmed.",
};

/** The validation shown: the backend's, downgraded to "unverified" while the league data is not current. */
export function shownValidation(c: Pick<PlanClaim, "validation">, freshness: ProvenanceStatus): { validation: Validation; reason: string | null } {
  if (c.validation === "legal" && freshness !== "current") {
    return { validation: "unverified", reason: FRESHNESS_REASON[freshness] ?? FRESHNESS_REASON.unknown! };
  }
  return { validation: c.validation, reason: null };
}

const money = (n: number) => `$${Number.isInteger(n) ? n : n.toFixed(2)}`;

/** The rules line: what the provider supplies, unknown spelled out. */
export function rulesText(r: PlanRules, provider: string): string {
  const parts: string[] = [];
  if (r.acquisition === "FAAB") {
    parts.push(r.faab_remaining !== null && r.faab_budget !== null
      ? `FAAB bidding: ${money(r.faab_remaining)} of ${money(r.faab_budget)} remaining`
      : "FAAB bidding: remaining budget unknown");
  } else if (r.acquisition === "PRIORITY") {
    parts.push(r.waiver_priority !== null ? `Waiver priority #${r.waiver_priority}` : "Waiver priority: your position is unknown");
  } else {
    parts.push(`Claim rules unknown: check ${provider}`);
  }
  parts.push(r.process_days?.display ?? "Processing days unknown");
  return parts.join(" · ");
}

function playerText(p: PlanPlayer | null, id: number, names: ReadonlyMap<number, string>): string {
  const name = p?.name ?? names.get(id) ?? `Player #${id}`;
  const meta = [p?.position, p?.pro_team].filter(Boolean).join(", ");
  return meta ? `${name} (${meta})` : name;
}

/**
 * The plan as plain text to paste into notes or a message. It says plainly that nothing has been submitted, and every
 * claim carries its check as shown on the page. `claims` is the device's plan; `res` the check of it (or null when the
 * check failed, and then every claim is "not checked").
 */
export function planText(args: {
  claims: readonly Claim[]; res: ClaimPlanResponse | null; freshness: ProvenanceStatus; provider: string;
  leagueName: string | null; teamName: string | null; names: ReadonlyMap<number, string>; today: string;
}): string {
  const { claims, res, freshness, provider, leagueName, teamName, names, today } = args;
  const byAdd = new Map((res?.claims ?? []).map((c) => [c.request.add, c]));
  const lines = [
    ["Waiver claim plan", leagueName, teamName, today].filter(Boolean).join(" · "),
    `NOT SUBMITTED: nothing here has been sent to ${provider}. Enter each claim in ${provider} yourself.`,
  ];
  if (res) lines.push(rulesText(res.rules, provider));
  lines.push("");
  claims.forEach((c, i) => {
    const r = byAdd.get(c.add);
    const sameRequest = r && r.request.drop === c.drop && r.request.bid === c.bid;
    const parts = [`${i + 1}. Add ${playerText(r?.add ?? null, c.add, names)}`];
    parts.push(c.drop !== null ? `drop ${playerText(r?.drop ?? null, c.drop, names)}` : "no drop");
    if (c.bid !== null) parts.push(`bid ${money(c.bid)}`);
    if (r && sameRequest && i < MAX_CLAIMS) {
      const shown = shownValidation(r, freshness);
      const why = shown.reason ?? r.reasons.find((x) => x.effect === shown.validation)?.message ?? null;
      parts.push(`${VALIDATION_LABEL[shown.validation]}${why && shown.validation !== "legal" ? `: ${why}` : ""}`);
    } else {
      parts.push(i >= MAX_CLAIMS ? `Not checked: only the first ${MAX_CLAIMS} claims are checked` : "Not checked");
    }
    lines.push(parts.join(" · "));
  });
  if (claims.length === 0) lines.push("No claims.");
  return lines.join("\n");
}
