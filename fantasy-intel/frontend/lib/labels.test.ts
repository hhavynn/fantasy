import assert from "node:assert/strict";
import test from "node:test";
import {
  normalizeProvider, ownershipAvailable, projectionName, projectionsPhrase, projectionWord, providerName, unnamedLeague, unnamedPlayer,
} from "./labels.ts";

test("providerName keeps ESPN wording and names Sleeper", () => {
  assert.equal(providerName("espn"), "ESPN");
  assert.equal(providerName("sleeper"), "Sleeper");
  assert.equal(providerName("Sleeper"), "Sleeper");
  assert.equal(providerName(undefined), "ESPN");
  assert.equal(providerName("yahoo"), "ESPN");
  assert.equal(normalizeProvider(" SLEEPER "), "sleeper");
  assert.equal(normalizeProvider(42), "espn");
});

test("projectionName reads switch values, API sources and backend labels", () => {
  assert.equal(projectionName("espn"), "ESPN");
  assert.equal(projectionName("ESPN"), "ESPN");
  assert.equal(projectionName("ours"), "Ours");
  assert.equal(projectionName("independent"), "Ours");
  assert.equal(projectionName("Ours"), "Ours");
  assert.equal(projectionName("Stored ESPN league-scoring projections"), "ESPN");
  assert.equal(projectionName("Fantasy Intel's own projections (Ours), in league scoring"), "Ours");
  assert.equal(projectionName(null), "ESPN");
  // a response with no projected number names no source (#254)
  assert.equal(projectionName("Unavailable"), "Unavailable");
  assert.equal(`no ${projectionWord("Unavailable")}projection`, "no projection");
  assert.equal(`no ${projectionWord(projectionName("Ours"))}projection`, "no Ours projection");
});

test("ownership and fallbacks", () => {
  assert.equal(ownershipAvailable("espn"), true);
  assert.equal(ownershipAvailable("sleeper"), false);
  assert.equal(unnamedLeague("espn", "4242"), "ESPN league 4242");
  assert.equal(unnamedLeague("sleeper", "1234567890"), "Sleeper league 1234567890");
  assert.equal(unnamedPlayer(15847), "Player #15847");
});

test("projectionsPhrase never names a source over a response without projections", () => {
  assert.equal(projectionsPhrase("ESPN"), "ESPN projections");
  assert.equal(projectionsPhrase("Ours"), "Ours projections");
  assert.equal(projectionsPhrase("Unavailable"), "no projections");
  assert.equal(projectionsPhrase(null), "no projections");
  assert.equal(projectionsPhrase(undefined), "no projections");
});
