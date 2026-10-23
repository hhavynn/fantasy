import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { PROFILE_LIMIT, attributionEligible, profileChange, profileMoversQuery } from "./profile-attribution.ts";
import type { MoversResponse } from "./projection-movers.ts";

// The synthetic movers fixture (invented players 900001...), relabelled as Fantasy Intel's own forecasts.
const espn = JSON.parse(readFileSync(new URL("./fixtures/projection-movers.synthetic.json", import.meta.url), "utf8")) as MoversResponse;
const ours: MoversResponse = { ...espn, source: "independent" };

test("the profile asks for Fantasy Intel's rest-of-season change of that one player", () => {
  const q = new URLSearchParams(profileMoversQuery(900004));
  assert.equal(q.get("source"), "independent");
  assert.equal(q.get("view"), "ros");
  assert.equal(q.get("limit"), String(PROFILE_LIMIT));
  assert.equal(q.get("player"), "900004");
  assert.equal(q.get("baseline"), null);
});

test("only the requested player's mover is shown, with a link that opens him on the movers page", () => {
  const c = profileChange(ours, 900005);
  assert.equal(c.kind, "mover");
  if (c.kind !== "mover") return;
  assert.equal(c.mover.espn_player_id, 900005);
  assert.equal(c.href, "/movers?source=independent&player=900005");
});

test("a player who is not in the response never gets another player's change", () => {
  const missing = { ...ours, requested_player: { espn_player_id: 1, status: "not_found" as const, reason: null } };
  const c = profileChange(missing, 1);
  assert.equal(c.kind, "unavailable");
  if (c.kind !== "unavailable") return;
  assert.equal(c.text, "That player has no forecast in either compared snapshot.");
  assert.equal(c.href, "/movers?source=independent");
  const notComparable = { ...ours, requested_player: { espn_player_id: 1, status: "not_comparable" as const, reason: "the earlier forecast has no row for him" } };
  assert.equal((profileChange(notComparable, 1) as { text: string }).text,
    "That player could not be compared: the earlier forecast has no row for him.");
  assert.equal(profileChange({ ...ours, requested_player: undefined }, 1).kind, "unavailable");
});

test("no comparison, a failed request, an ESPN answer or another contract is unavailable, never a zero change", () => {
  const none = profileChange({ ...ours, available: false, reason: "Only one stored forecast exists." }, 900005);
  assert.deepEqual(none, { kind: "unavailable", text: "Only one stored forecast exists.", href: "/movers?source=independent" });
  assert.equal(profileChange(null, 900005).kind, "unavailable");
  assert.equal(profileChange(espn, 900005).kind, "unavailable");
  assert.equal(profileChange({ ...ours, contract: "projection-movers/v0" }, 900005).kind, "unavailable");
});

test("forecast changes cover the skill positions only", () => {
  assert.deepEqual(["QB", "RB", "WR", "TE", "K", "D/ST", null].map(attributionEligible), [true, true, true, true, false, false, false]);
});
