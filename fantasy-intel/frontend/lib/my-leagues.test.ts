import assert from "node:assert/strict";
import test from "node:test";
import {
  cleanCookies, defaultSeason, leagueName, leagueState, multileagueUiEnabled, parseLeagueInput, parseMyLeagues,
  parseProviderChoice, parseSleeperLeagueInput,
  validSeason,
} from "./my-leagues.ts";

const ACCESS = {
  league_id: "lg_abcdefghijklmnop", provider: "espn", external_league_id: "4242", season: 2026, name: null,
  visibility: "private", status: "active", role: "admin", pinned: false, perspective_team_id: "3",
  sync_state: "ready", data: { data_version: "v", global_version: "g", committed_at: "2026-10-21T12:00:00Z" },
};

test("the flag is exactly 1", () => {
  assert.equal(multileagueUiEnabled("1"), true);
  for (const v of [undefined, "", "0", "true"]) assert.equal(multileagueUiEnabled(v), false);
});

test("the backend's league list becomes the page's view model; malformed rows are dropped", () => {
  const [l, ...rest] = parseMyLeagues({ leagues: [ACCESS, { league_id: 7 }, null] });
  assert.equal(rest.length, 0);
  assert.deepEqual(l, { id: "lg_abcdefghijklmnop", provider: "espn", externalId: "4242", season: 2026, name: null,
    visibility: "private", status: "active", role: "admin", pinned: false, teamId: "3", syncState: "ready",
    syncIssue: null, committedAt: "2026-10-21T12:00:00Z" });
  assert.equal(leagueName(l), "ESPN league 4242");
  assert.deepEqual(parseMyLeagues({}), []);
});

test("the league's state says what readers see and why", () => {
  assert.equal(leagueState({ status: "stale", syncState: "ready", visibility: "private" }).label, "Sync paused");
  assert.equal(leagueState({ status: "active", syncState: "first_sync_running", visibility: "public" }).tone, "info");
  assert.match(leagueState({ status: "unavailable", syncState: "ready", visibility: "private" }).detail, /privately/);
  assert.equal(leagueState({ status: "active", syncState: "ready", visibility: "public" }).label, "Up to date");
});

test("a league id or its ESPN URL", () => {
  assert.deepEqual(parseLeagueInput(" 4242 "), { leagueId: "4242", season: null });
  assert.deepEqual(parseLeagueInput("https://fantasy.espn.com/football/league?leagueId=4242&seasonId=2025"),
    { leagueId: "4242", season: 2025 });
  assert.equal(parseLeagueInput("https://fantasy.espn.com/football/"), null);
  assert.equal(validSeason("2026"), 2026);
  assert.equal(validSeason("26"), null);
  assert.equal(defaultSeason(new Date("2026-03-01T00:00:00Z")), 2025);
  assert.equal(defaultSeason(new Date("2026-10-01T00:00:00Z")), 2026);
});

test("cookies are trimmed and the SWID braced; anything else is refused", () => {
  const s2 = "AEB" + "x".repeat(40);
  assert.deepEqual(cleanCookies(` ${s2} `, "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"),
    { espn_s2: s2, swid: "{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}" });
  assert.equal(cleanCookies("short", "{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}"), null);
  assert.equal(cleanCookies(s2, "not-a-swid"), null);
});

test("parseSleeperLeagueInput takes a long id or a sleeper.com league address", () => {
  const id = "1180208749051539456";
  assert.deepEqual(parseSleeperLeagueInput(` ${id} `), { leagueId: id });
  assert.deepEqual(parseSleeperLeagueInput(`https://sleeper.com/leagues/${id}/predraft`), { leagueId: id });
  assert.deepEqual(parseSleeperLeagueInput(`sleeper.com/leagues/${id}`), { leagueId: id });
  assert.deepEqual(parseSleeperLeagueInput(`https://sleeper.app/leagues/${id}?tab=matchup`), { leagueId: id });
  assert.deepEqual(parseSleeperLeagueInput("123456"), { leagueId: "123456" });
  assert.equal(parseSleeperLeagueInput("12345"), null);                       // too short
  assert.equal(parseSleeperLeagueInput("1".repeat(21)), null);                 // too long
  assert.equal(parseSleeperLeagueInput("https://fantasy.espn.com/football/league?leagueId=4242"), null);
  assert.equal(parseSleeperLeagueInput(`https://evil.example/sleeper.com/leagues/${id}`), null);
  assert.equal(parseSleeperLeagueInput(""), null);
});

test("parseProviderChoice: only an explicit sleeper is Sleeper", () => {
  assert.equal(parseProviderChoice("sleeper"), "sleeper");
  assert.equal(parseProviderChoice("espn"), "espn");
  assert.equal(parseProviderChoice(null), "espn");
});

test("league names and states are provider-aware, ESPN wording unchanged", () => {
  assert.equal(leagueName({ name: null, externalId: "4242", provider: "espn" }), "ESPN league 4242");
  assert.equal(leagueName({ name: "  ", externalId: "118020874905", provider: "sleeper" }), "Sleeper league 118020874905");
  assert.equal(leagueName({ name: "Dynasty", externalId: "1", provider: "sleeper" }), "Dynasty");
  const espnStale = leagueState({ status: "stale", syncState: "ready", visibility: "private", provider: "espn" });
  assert.match(espnStale.detail, /ESPN cookies/);
  const sleeperStale = leagueState({ status: "stale", syncState: "ready", visibility: "public", provider: "sleeper" });
  assert.equal(sleeperStale.label, "Sync paused");
  assert.doesNotMatch(sleeperStale.detail, /ESPN|cookie/i);
  const gone = leagueState({ status: "unavailable", syncState: "ready", visibility: "public", provider: "sleeper" });
  assert.equal(gone.label, "Unavailable on Sleeper");
  assert.doesNotMatch(gone.detail, /ESPN|cookie/i);
  assert.equal(leagueState({ status: "unavailable", syncState: "ready", visibility: "public" }).label, "Unavailable on ESPN");
});
