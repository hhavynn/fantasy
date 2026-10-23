import assert from "node:assert/strict";
import test from "node:test";
import { addable, injuryText, rosterStatus } from "./roster-status.ts";

test("ESPN roster states: rostered by a named team, on waivers, or a free agent", () => {
  assert.deepEqual(rosterStatus("ONTEAM", "Team 1"), { kind: "rostered", text: "Team 1", title: "Rostered by Team 1" });
  assert.equal(rosterStatus("ONTEAM", null).text, "Rostered");
  assert.equal(rosterStatus("WAIVERS", null).kind, "waivers");
  assert.equal(rosterStatus("FREEAGENT", null, "espn").text, "Free agent");
});

test("Sleeper never says free agent: an unrostered player may still be on waivers", () => {
  const s = rosterStatus("FREEAGENT", null, "sleeper");
  assert.equal(s.kind, "unrostered");
  assert.equal(s.text, "Not rostered");
  assert.match(s.title, /does not say whether he is on waivers/);
  assert.equal(rosterStatus("ONTEAM", "Team 2", "sleeper").text, "Team 2");
});

test("a missing or unknown status is unknown, never free; a team name alone still means rostered", () => {
  for (const a of [null, undefined, "", "OTHER"]) assert.equal(rosterStatus(a, null).kind, "unknown", String(a));
  assert.equal(rosterStatus(null, "Team 4").kind, "rostered");
  assert.equal(addable("unknown"), false);
  assert.equal(addable("rostered"), false);
  for (const k of ["free", "waivers", "unrostered"] as const) assert.equal(addable(k), true, k);
});

test("injury designations in words; ACTIVE and NORMAL mean none", () => {
  assert.equal(injuryText("OUT"), "Out");
  assert.equal(injuryText("INJURY_RESERVE"), "IR");
  assert.equal(injuryText("QUESTIONABLE"), "Questionable");
  assert.equal(injuryText("PUP_LIST"), "Pup list");
  for (const s of [null, undefined, "", "ACTIVE", "NORMAL", " active "]) assert.equal(injuryText(s), null, String(s));
});
