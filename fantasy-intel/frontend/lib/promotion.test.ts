import assert from "node:assert/strict";
import test from "node:test";
import { clearsBar, coversLaterGames, matchedProgress, signedPct } from "./promotion.ts";

test("matchedProgress is the collected share of the requirement, capped at 1", () => {
  assert.equal(matchedProgress(0, 300), 0);
  assert.equal(matchedProgress(150, 300), 0.5);
  assert.equal(matchedProgress(450, 300), 1);
  assert.equal(matchedProgress(5, 0), 0);
});

test("signedPct always carries a sign so direction never depends on colour", () => {
  assert.equal(signedPct(0.0578), "+5.8%");
  assert.equal(signedPct(-0.0056), "−0.6%");
  assert.equal(signedPct(0.00001), "0.0%");
  assert.equal(signedPct(null), null);
});

test("clearsBar compares a stored improvement with the frozen bar", () => {
  assert.equal(clearsBar(0.0578, 0.03), true);
  assert.equal(clearsBar(0.0276, 0.03), false);
  assert.equal(clearsBar(0.03, 0.03), true);
  assert.equal(clearsBar(null, 0.03), null);
});

test("coversLaterGames: a snapshot after the first kickoff still counts for games that start later", () => {
  const last = "2026-10-20T00:15:00+00:00";
  assert.equal(coversLaterGames("2026-10-16T08:00:00+00:00", last), true);
  assert.equal(coversLaterGames("2026-10-20T01:00:00+00:00", last), false);
  assert.equal(coversLaterGames(null, last), false);
  assert.equal(coversLaterGames("2026-10-16T08:00:00+00:00", null), false);
});
