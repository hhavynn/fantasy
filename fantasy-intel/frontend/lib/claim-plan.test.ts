import assert from "node:assert/strict";
import test from "node:test";
import {
  MAX_CLAIMS, claimPlanBackendPath, claimPlanKey, claimPlanSrc, leagueFreshness, moveClaim, parseBid, parseClaimPlan,
  planText, readClaimPlan, reconcile, rulesText, serializeClaimPlan, shownValidation, updateClaim, writeClaimPlan,
  type Claim, type ClaimPlanResponse, type PlanRules,
} from "./claim-plan.ts";

const A = "lg_aaaaaaaaaaaaaaaa";
const B = "lg_bbbbbbbbbbbbbbbb";
const c = (add: number, drop: number | null = null, bid: number | null = null): Claim => ({ add, drop, bid });

function memory() {
  const map = new Map<string, string>();
  return { map, io: { getItem: (k: string) => map.get(k) ?? null, setItem: (k: string, v: string) => { map.set(k, v); } } };
}

test("a plan is keyed by canonical league and team; unscoped, malformed or teamless plans are never stored", () => {
  assert.equal(claimPlanKey(A, 2), "fi:waiver-claim-plan:v1:lg_aaaaaaaaaaaaaaaa:2");
  assert.notEqual(claimPlanKey(A, 2), claimPlanKey(B, 2));
  assert.notEqual(claimPlanKey(A, 2), claimPlanKey(A, 3));
  for (const [lg, team] of [[null, 2], ["12345", 2], ["lg_bad", 2], [A, null], [A, -1], [A, 1.5]] as const)
    assert.equal(claimPlanKey(lg, team as number | null), null);
});

test("order, candidate, drop and bid survive a reload on the same league and team", () => {
  const { io } = memory();
  const plan = [c(309, 206, 15), c(209), c(-16002, 207, 0)];
  assert.equal(writeClaimPlan(io, A, 2, plan), true);
  assert.deepEqual(readClaimPlan(io, A, 2), { claims: plan, available: true });
});

test("another league or team never sees the plan, even a value copied under its key", () => {
  const { io, map } = memory();
  writeClaimPlan(io, A, 2, [c(309, 206, 15)]);
  assert.deepEqual(readClaimPlan(io, B, 2).claims, []);
  assert.deepEqual(readClaimPlan(io, A, 3).claims, []);
  // a payload from league A pasted under league B's key is refused: it names its own league and team
  map.set(claimPlanKey(B, 2)!, map.get(claimPlanKey(A, 2)!)!);
  assert.deepEqual(readClaimPlan(io, B, 2).claims, []);
  assert.equal(writeClaimPlan(io, null, 2, [c(1)]), false);
  assert.equal(writeClaimPlan(io, A, null, [c(1)]), false);
});

test("malformed stored plans are refused whole", () => {
  const ok = (claims: unknown) => JSON.stringify({ v: 1, league: A, team: 2, claims });
  for (const raw of ["{", "null", "[]", JSON.stringify({ v: 2, league: A, team: 2, claims: [] }), ok("x"),
    ok([{ add: 1.5, drop: null, bid: null }]), ok([{ add: 309, drop: 309, bid: null }]), ok([{ add: 309, drop: null, bid: -1 }]),
    ok([{ add: 309, drop: null, bid: 1.5 }]), ok([{ add: 309, drop: null, bid: 200000 }]), ok([{ add: "309", drop: null, bid: null }]),
    ok([{ add: 309, drop: null, bid: null }, { add: 309, drop: 1, bid: null }]), ok([null])])
    assert.deepEqual(parseClaimPlan(raw, A, 2), [], raw);
  assert.deepEqual(parseClaimPlan(serializeClaimPlan(A, 2, [c(309, null, 0)]), A, 2), [c(309, null, 0)]);
});

test("unavailable storage fails safely and an unscoped read never touches storage", () => {
  let reads = 0;
  const io = { getItem: () => { reads++; throw new Error("denied"); }, setItem: () => { throw new Error("quota"); } };
  assert.deepEqual(readClaimPlan(io, A, 2), { claims: [], available: false });
  assert.equal(writeClaimPlan(io, A, 2, [c(1)]), false);
  assert.deepEqual(readClaimPlan(io, null, 2), { claims: [], available: false });
  assert.equal(reads, 1);
});

test("the plan follows the shortlist: kept claims keep their order, new players join last, removed ones leave", () => {
  const saved = [c(3, 9, 5), c(1), c(2, 8)];
  assert.deepEqual(reconcile(saved, [1, 2, 3, 4]), [c(3, 9, 5), c(1), c(2, 8), c(4)]);
  assert.deepEqual(reconcile(saved, [2, 4]), [c(2, 8), c(4)]);
  assert.deepEqual(reconcile([], []), []);
});

test("reordering and edits never change the input and refuse a self-drop or a bad bid", () => {
  const plan = [c(1), c(2), c(3)];
  assert.deepEqual(moveClaim(plan, 2, 0).map((x) => x.add), [3, 1, 2]);
  assert.deepEqual(moveClaim(plan, 0, 9).map((x) => x.add), [2, 3, 1]);
  assert.deepEqual(moveClaim(plan, 7, 0), plan);
  assert.deepEqual(plan.map((x) => x.add), [1, 2, 3]);
  assert.deepEqual(updateClaim(plan, 2, { drop: 9, bid: 4 })[1], c(2, 9, 4));
  assert.deepEqual(updateClaim(plan, 2, { drop: 2 })[1], c(2));
  assert.deepEqual(updateClaim(plan, 2, { bid: -3 })[1], c(2));
});

test("bids are whole dollars or empty", () => {
  assert.deepEqual(parseBid(""), { bid: null, ok: true });
  assert.deepEqual(parseBid(" 15 "), { bid: 15, ok: true });
  assert.deepEqual(parseBid("0"), { bid: 0, ok: true });
  for (const t of ["1.5", "-2", "abc", "1e3", "1000000"]) assert.equal(parseBid(t).ok, false, t);
});

test("the check is always requested under the league, in claim order, and only well-formed claims reach the backend", () => {
  const src = claimPlanSrc(A, 2, [c(309, 206, 15), c(209), c(-16002, null, 0)]);
  assert.equal(src, `/l/${A}/api/waiver-claim-plan?team_id=2&claims=309%3A206%3A15%2C209%3A%3A%2C-16002%3A%3A0`);
  // another order is another request (the league-scoped response cache sorts query pairs, not one value's parts)
  assert.notEqual(claimPlanSrc(A, 2, [c(209), c(309, 206, 15)]), claimPlanSrc(A, 2, [c(309, 206, 15), c(209)]));
  const many = claimPlanSrc(A, 2, Array.from({ length: MAX_CLAIMS + 3 }, (_, i) => c(100 + i)));
  assert.equal(new URL(many, "http://x").searchParams.get("claims")!.split(",").length, MAX_CLAIMS);
  const path = claimPlanBackendPath(new URL(src, "http://x").searchParams);
  assert.equal(path, "/api/waivers/claim-plan?team_id=2&claims=309%3A206%3A15%2C209%3A%3A%2C-16002%3A%3A0");
  for (const q of ["claims=1", "team_id=x&claims=1", "team_id=2&claims=1;drop", "team_id=2&claims=1:2:3:4", "team_id=2&claims=1,,2",
    `team_id=2&claims=${Array.from({ length: MAX_CLAIMS + 1 }, (_, i) => i + 1).join(",")}`])
    assert.equal(claimPlanBackendPath(new URLSearchParams(q)), null, q);
  assert.equal(claimPlanBackendPath(new URLSearchParams("team_id=2&league_id=9&claims=1")), "/api/waivers/claim-plan?team_id=2&claims=1");
  assert.equal(claimPlanBackendPath(new URLSearchParams("team_id=2")), "/api/waivers/claim-plan?team_id=2&claims=");
});

const NOW = Date.parse("2026-10-23T12:00:00Z");
const league = (captured: string | null, stale: string | null = null) => ({
  contract: "signal-provenance.v1",
  signals: [{ signal: "league" as const, label: "ESPN rosters", source: "espn" as const, captured_at: captured, snapshot_version: null,
    coverage: null, reason_unavailable: null, stale_reason: stale, stale_after_hours: 36 }],
});

test("a legal claim is shown as passing only while the league data is current", () => {
  assert.equal(leagueFreshness(league("2026-10-23T08:00:00Z"), NOW), "current");
  assert.equal(leagueFreshness(league("2026-10-20T08:00:00Z"), NOW), "stale");
  assert.equal(leagueFreshness(league(null), NOW), "unknown");
  assert.equal(leagueFreshness(null, NOW), "unknown");
  assert.deepEqual(shownValidation({ validation: "legal" }, "current"), { validation: "legal", reason: null });
  for (const f of ["stale", "unknown", "unavailable", "partial"] as const) {
    const s = shownValidation({ validation: "legal" }, f);
    assert.equal(s.validation, "unverified", f);
    assert.ok(s.reason, f);
  }
  assert.equal(shownValidation({ validation: "invalid" }, "current").validation, "invalid");
});

const RULES: PlanRules = {
  acquisition: "FAAB", is_faab: true, faab_budget: 100, faab_spent: 20, faab_remaining: 80, waiver_priority: null,
  waiver_type: { value: "faab", display: "FAAB bidding", reason: null }, waiver_period: null,
  process_days: { value: null, display: null, reason: "Not in the synced settings." }, unknown: [],
};

test("the rules line says only what the provider supplies", () => {
  assert.equal(rulesText(RULES, "ESPN"), "FAAB bidding: $80 of $100 remaining · Processing days unknown");
  assert.equal(rulesText({ ...RULES, faab_remaining: null }, "ESPN"), "FAAB bidding: remaining budget unknown · Processing days unknown");
  assert.equal(rulesText({ ...RULES, acquisition: "PRIORITY", waiver_priority: 3, process_days: { value: ["Wednesday"], display: "Claims process on Wednesday", reason: null } }, "Sleeper"),
    "Waiver priority #3 · Claims process on Wednesday");
  assert.equal(rulesText({ ...RULES, acquisition: "UNKNOWN" }, "Sleeper"), "Claim rules unknown: check Sleeper · Processing days unknown");
});

function response(claims: ClaimPlanResponse["claims"]): ClaimPlanResponse {
  return {
    contract: "waiver-claim-plan/v1", submitted: false, status: "evaluated", max_claims: MAX_CLAIMS, team: { id: 2, name: "Team 2" },
    rules: RULES, roster: { active_capacity: 10, active_before: 9, open_spots_before: 1, drop_options: [] }, claims,
    summary: { claims: claims.length, legal: 1, unverified: 1, invalid: 0, total_bid: 15, remaining_after_bids: 65, conflicts: [] },
  };
}
const planned = (rank: number, request: Claim, validation: "legal" | "unverified" | "invalid", message?: string) => ({
  rank, request, validation,
  reasons: message ? [{ code: "X", message, effect: validation, player_id: request.add }] : [],
  add: { espn_player_id: request.add, name: `P${request.add}`, position: "WR", pro_team: "BUF", roster_status: "waivers" },
  drop: request.drop === null ? null : { espn_player_id: request.drop, name: `P${request.drop}`, position: "RB", pro_team: "KC", roster_status: "own_roster" },
  claim_kind: "waiver_claim" as const, simulation_status: "evaluated" as const,
  roster: { active_after: 10, open_spots_after: 0, legal: true, drop_required: false }, suggested_drop: null, impact: null,
});

test("the plain-text plan says it is not submitted and carries each claim's check as shown", () => {
  const claims = [c(309, 206, 15), c(209), c(405)];
  const res = response([planned(1, claims[0], "legal"), planned(2, claims[1], "unverified", "Claim 1 uses the open spot."),
    planned(3, c(405, 206), "legal")]);
  const text = planText({ claims, res, freshness: "current", provider: "ESPN", leagueName: "Fixture League", teamName: "Team 2",
    names: new Map(), today: "2026-10-23" });
  const lines = text.split("\n");
  assert.equal(lines[0], "Waiver claim plan · Fixture League · Team 2 · 2026-10-23");
  assert.match(lines[1], /^NOT SUBMITTED: nothing here has been sent to ESPN/);
  assert.equal(lines[2], "FAAB bidding: $80 of $100 remaining · Processing days unknown");
  assert.equal(lines[4], "1. Add P309 (WR, BUF) · drop P206 (RB, KC) · bid $15 · Checks pass");
  assert.equal(lines[5], "2. Add P209 (WR, BUF) · no drop · Needs review: Claim 1 uses the open spot.");
  // a check of a different request (the device changed the drop since) is never attached to this claim
  assert.equal(lines[6], "3. Add P405 (WR, BUF) · no drop · Not checked");
  const stale = planText({ claims, res, freshness: "stale", provider: "ESPN", leagueName: null, teamName: null, names: new Map(), today: "" });
  assert.match(stale.split("\n")[4], /^1\. .* · Needs review: The league data is stale/);
  const failed = planText({ claims, res: null, freshness: "unknown", provider: "Sleeper", leagueName: null, teamName: null,
    names: new Map([[309, "Waiver WR"]]), today: "" });
  assert.doesNotMatch(failed, /ESPN|Checks pass/);
  assert.match(failed, /1\. Add Waiver WR · drop Player #206 · bid \$15 · Not checked/);
});
