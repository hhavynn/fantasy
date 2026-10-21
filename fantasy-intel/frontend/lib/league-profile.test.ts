import assert from "node:assert/strict";
import test from "node:test";
import { ESPN_PROFILE, SLEEPER_PROFILE, parseLeagueProfile, profileForProvider } from "./league-profile.ts";

test("parseLeagueProfile reads the endpoint's payload", () => {
  assert.deepEqual(parseLeagueProfile({ provider: "espn", default_source: "espn", provider_projections: true }), ESPN_PROFILE);
  assert.deepEqual(parseLeagueProfile({ provider: "sleeper", default_source: "independent", provider_projections: false }), SLEEPER_PROFILE);
});

test("parseLeagueProfile falls back to the ESPN profile, or to what the provider implies", () => {
  assert.deepEqual(parseLeagueProfile(null), ESPN_PROFILE);
  assert.deepEqual(parseLeagueProfile("nope"), ESPN_PROFILE);
  assert.deepEqual(parseLeagueProfile([]), ESPN_PROFILE);
  assert.deepEqual(parseLeagueProfile({}), ESPN_PROFILE);
  assert.deepEqual(parseLeagueProfile(null, "sleeper"), SLEEPER_PROFILE);
  assert.deepEqual(parseLeagueProfile({ provider: "sleeper" }), SLEEPER_PROFILE);
  assert.deepEqual(parseLeagueProfile({ provider: "espn", default_source: "bogus" }), ESPN_PROFILE);
  assert.deepEqual(profileForProvider("sleeper"), SLEEPER_PROFILE);
  assert.deepEqual(profileForProvider(undefined), ESPN_PROFILE);
});

test("ESPN numbers are never the default where the provider has none", () => {
  assert.deepEqual(parseLeagueProfile({ provider: "sleeper", default_source: "espn", provider_projections: false }), SLEEPER_PROFILE);
  // An ESPN league that defaults to Ours keeps its ESPN projections available.
  assert.deepEqual(parseLeagueProfile({ provider: "espn", default_source: "independent", provider_projections: true }),
    { provider: "espn", defaultSource: "independent", providerProjections: true });
});
