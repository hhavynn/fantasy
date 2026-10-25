import assert from "node:assert/strict";
import test from "node:test";
import {
  DEFAULT_SHARE, bidVsRange, budgetText, faabBackendPath, faabClaimParam, faabSrc, isShare, money, observedText, rangeText,
  type FaabResponse,
} from "./faab.ts";

const A = "lg_aaaaaaaaaaaaaaaa";

test("the guidance URL is always under the league, carries the plan in order without bids, and the share", () => {
  const src = faabSrc(A, 2, [{ add: 309, drop: 206 }, { add: 209, drop: null }], 50);
  assert.ok(src.startsWith(`/l/${A}/api/waiver-faab?`));
  const q = new URL(src, "http://x").searchParams;
  assert.equal(q.get("team_id"), "2");
  assert.equal(q.get("claims"), "309:206,209");
  assert.equal(q.get("share"), "50");
  // an out-of-range share falls back to the default rather than reaching the backend
  assert.equal(new URL(faabSrc(A, 2, [], 0), "http://x").searchParams.get("share"), String(DEFAULT_SHARE));
  assert.equal(faabClaimParam({ add: -16002, drop: 107 }), "-16002:107");
});

test("only a well-formed team, plan and share are forwarded to the backend", () => {
  const ok = faabBackendPath(new URLSearchParams({ team_id: "2", claims: "309:206,209", share: "25" }));
  assert.equal(ok, "/api/waivers/faab?team_id=2&claims=309%3A206%2C209&share=25");
  assert.equal(faabBackendPath(new URLSearchParams({ team_id: "2" })), "/api/waivers/faab?team_id=2&claims=&share=25");
  for (const bad of [
    { claims: "309" },                                   // no team
    { team_id: "x", claims: "309" },
    { team_id: "2", claims: "309:206:5" },               // a bid: guidance never takes one
    { team_id: "2", claims: "309,,209" },
    { team_id: "2", share: "0" },
    { team_id: "2", share: "101" },
    { team_id: "2", share: "2.5" },
    { team_id: "2", claims: Array.from({ length: 13 }, (_, i) => String(300 + i)).join(",") },
  ]) assert.equal(faabBackendPath(new URLSearchParams(bad as Record<string, string>)), null, JSON.stringify(bad));
  assert.equal(isShare(25), true);
  assert.equal(isShare(0), false);
});

test("ranges, bid comparisons and money read plainly", () => {
  assert.equal(rangeText({ low: 5, high: 13 }), "$5–$13");
  assert.equal(rangeText({ low: 0, high: 0 }), "$0");
  assert.equal(money(80), "$80");
  assert.equal(money(5.333), "$5.33");
  assert.equal(bidVsRange(20, { low: 5, high: 13 }), "above");
  assert.equal(bidVsRange(0, { low: 5, high: 13 }), "below");   // a $0 bid is a real bid
  assert.equal(bidVsRange(5, { low: 5, high: 13 }), "within");
  assert.equal(bidVsRange(null, { low: 5, high: 13 }), null);
  assert.equal(bidVsRange(5, null), null);
});

const budget = (over: Partial<FaabResponse["budget"]> = {}): FaabResponse["budget"] => ({
  acquisition: "FAAB", budget: 100, spent: 20, remaining: 80, minimum_bid: null, weeks_left: 3,
  even_split_per_week: 26.67, other_teams: { teams: 2, known: 2, max_remaining: 100 }, ...over,
});

test("the budget line uses synced values only and never shows an unknown budget as 0", () => {
  assert.equal(budgetText(budget()), "$80 of $100 FAAB left (synced) · even split $26.67 a week over 3 weeks · minimum bid not synced");
  const unknown = budgetText(budget({ remaining: null, spent: null, even_split_per_week: null }));
  assert.match(unknown, /Remaining FAAB unknown/);
  assert.doesNotMatch(unknown, /\$0/);
  assert.equal(budgetText(budget({ minimum_bid: 0 })).endsWith("minimum bid $0"), true);
  assert.equal(budgetText(budget({ acquisition: "PRIORITY" })), "");
});

test("observed bids are labelled as observed and compared only with enough of them", () => {
  const stats = { position: "WR", count: 4, median: 9, min: 2, max: 30, median_share_pct: 9 };
  assert.equal(observedText({ ...stats, relation: "below" }, 3),
    "Observed: 4 winning WR bids in this league this season, median $9, highest $30. This range is below the median.");
  assert.match(observedText({ ...stats, count: 1, relation: null }, 3)!, /1 winning WR bid in .*Too few to compare\.$/);
  assert.equal(observedText(null, 3), null);
});
