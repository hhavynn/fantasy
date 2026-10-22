import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { parseDemoLeague, record } from "./demo-league.ts";

const raw = JSON.parse(readFileSync(new URL("./fixtures/demo-league.synthetic.json", import.meta.url), "utf8"));

// The fixture league's invented names (backend/tests/espn_fixture.py): NATO-alphabet players, numbered teams.
const SYNTHETIC_PLAYER = /^(Alpha|Bravo|Charlie|Delta|Echo|Foxtrot|Golf|Hotel|India|Juliet|Kilo|Lima|Mike|November|Oscar|Papa|Quebec|Romeo|Sierra|Tango|Uniform|Victor|Whiskey|X-ray|Yankee|Zulu|Waiver) (QB|RB|WR|TE|K|D\/ST)$/;

test("the bundled demo is the synthetic fixture league, complete enough to show a decision", () => {
  const demo = parseDemoLeague(raw);
  assert.ok(demo);
  assert.equal(demo.league.name, "Fixture League");
  assert.match(demo.team, /^Team \d+$/);
  assert.ok(demo.lineup.start.length > 0 && demo.lineup.bench.length > 0);
  assert.ok((demo.lineup.projectedGain ?? 0) > 0);
  assert.equal(demo.standings.filter((t) => t.mine).length, 1);
  assert.ok(demo.waivers.length > 0);
  for (const p of [...demo.lineup.start, ...demo.lineup.bench, ...demo.waivers]) assert.match(p.name, SYNTHETIC_PLAYER);
  for (const t of demo.standings) assert.match(t.team, /^Team \d+$/);
});

test("the demo carries only allowlisted fields: no manager ids, owner names, cookies or tokens", () => {
  const text = JSON.stringify(raw);
  for (const banned of ["owner", "swid", "espn_s2", "token", "cookie", "m_", "league_id", "email"]) {
    assert.ok(!text.toLowerCase().includes(banned), banned);
  }
});

test("a malformed demo is rejected rather than half-rendered", () => {
  assert.equal(parseDemoLeague(null), null);
  assert.equal(parseDemoLeague({ league: { name: "x" }, lineup: {} }), null);
  assert.equal(record({ wins: 2, losses: 1, ties: 0 }), "2-1");
  assert.equal(record({ wins: 2, losses: 1, ties: 1 }), "2-1-1");
});
