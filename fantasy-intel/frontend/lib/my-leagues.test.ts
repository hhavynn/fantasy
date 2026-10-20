import assert from "node:assert/strict";
import test from "node:test";
import {
  cleanCookies, defaultSeason, leagueName, leagueState, multileagueUiEnabled, parseLeagueInput, parseMyLeagues,
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
    committedAt: "2026-10-21T12:00:00Z" });
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
