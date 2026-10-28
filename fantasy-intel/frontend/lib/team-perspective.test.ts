import assert from "node:assert/strict";
import { test } from "node:test";
import { ALL_NAV_PAGES } from "./navigation.ts";
import {
  isTeamNotFound, isTeamScoped, isTeamScopedView, isUnknownTeam, navTeam, needsDefaultTeam, parseStoredTeam, perspectiveHref, readTeamSelection, selectedTeamId, teamOptionLabel, teamParamFrom,
  TEAM_SCOPED_PATHS, withTeam, withTeamId,
} from "./team-perspective.ts";

test("a missing default team gets a picker without surfacing an operator hint", () => {
  assert.equal(needsDefaultTeam({ok:false,status:404,error:"Your team was not identified. Set ESPN_MY_TEAM_ID or pass team_id."}),true);
  assert.equal(needsDefaultTeam({ok:false,status:422,error:"No team is marked as yours in this league."}),true);
  assert.equal(needsDefaultTeam({ok:false,status:404,error:"League not found"}),false);
  assert.equal(needsDefaultTeam({ok:false,status:404,error:"Team 99 not found in this league."}),false);
  assert.equal(needsDefaultTeam({ok:true}),false);
});

test("no team in the URL keeps the owner's default", () => {
  assert.deepEqual(readTeamSelection({}), { kind: "default" });
  assert.deepEqual(readTeamSelection({ team: "" }), { kind: "default" });
  assert.equal(selectedTeamId(readTeamSelection({})), null);
  assert.equal(withTeamId("/api/overview", readTeamSelection({})), "/api/overview");
});

test("?team= selects a team; the legacy ?team_id= still works but ?team wins", () => {
  assert.deepEqual(readTeamSelection({ team: "7" }), { kind: "team", id: 7 });
  assert.deepEqual(readTeamSelection({ team_id: "3" }), { kind: "team", id: 3 });
  assert.deepEqual(readTeamSelection({ team: "4", team_id: "3" }), { kind: "team", id: 4 });
  assert.deepEqual(readTeamSelection({ team: ["5", "6"] }), { kind: "team", id: 5 });
});

test("a team param that is not an id is invalid and never reaches the backend", () => {
  assert.deepEqual(readTeamSelection({ team: "abc" }), { kind: "invalid", raw: "abc" });
  assert.deepEqual(readTeamSelection({ team: "-1" }), { kind: "invalid", raw: "-1" });
  assert.deepEqual(readTeamSelection({ team: "1;drop" }), { kind: "invalid", raw: "1;drop" });
  assert.equal(selectedTeamId(readTeamSelection({ team: "abc" })), null);
});

test("backend paths get team_id, keeping their own query", () => {
  const sel = readTeamSelection({ team: "9" });
  assert.equal(withTeamId("/api/overview", sel), "/api/overview?team_id=9");
  assert.equal(withTeamId("/api/waivers?view=BEST&limit=3", sel), "/api/waivers?view=BEST&limit=3&team_id=9");
  assert.equal(withTeamId("/api/matchup?team_id=2", 9), "/api/matchup?team_id=9");      // never two teams in one call
});

test("every team-scoped page is a nav page, and league-wide pages are not team-scoped", () => {
  const nav = new Set(ALL_NAV_PAGES.map((p) => p.href));
  for (const p of TEAM_SCOPED_PATHS) assert.ok(nav.has(p), p);
  for (const p of ["/players", "/research", "/projection-performance", "/weekly-recap", "/managers"]) {
    assert.equal(isTeamScoped(p), false, p);
  }
  assert.equal(isTeamScoped("/waivers/"), true);
});

test("links keep the selected team only between team-scoped pages", () => {
  assert.equal(withTeam("/waivers", "4"), "/waivers?team=4");
  assert.equal(withTeam("/waivers?pos=RB", "4"), "/waivers?pos=RB&team=4");
  assert.equal(withTeam("/", "4"), "/?team=4");
  assert.equal(withTeam("/players", "4"), "/players");
  assert.equal(withTeam("/managers/3", "4"), "/managers/3");
  assert.equal(withTeam("/waivers?team=2", "4"), "/waivers?team=2");          // a link naming a team keeps it
  assert.equal(withTeam("/waivers", null), "/waivers");
  assert.equal(withTeam("https://espn.com/waivers", "4"), "https://espn.com/waivers");
  assert.equal(withTeam("/start-sit#moves", "4"), "/start-sit?team=4#moves");
});

test("the current team is read from the URL for propagation, ignoring junk", () => {
  assert.equal(teamParamFrom(new URLSearchParams("team=6&week=3")), "6");
  assert.equal(teamParamFrom(new URLSearchParams("team_id=6")), "6");
  assert.equal(teamParamFrom(new URLSearchParams("team=x")), null);
  assert.equal(teamParamFrom(new URLSearchParams("")), null);
  assert.equal(teamParamFrom(null), null);
});

test("switching team keeps view params, drops the old team's own params, and the default has no param", () => {
  const sp = { team: "2", week: "6", pos: "RB", opponent: "5", offset: "25", vs: "3" };
  assert.equal(perspectiveHref("/waivers", sp, 4), "/waivers?week=6&pos=RB&team=4");
  assert.equal(perspectiveHref("/waivers", sp, null), "/waivers?week=6&pos=RB");
  assert.equal(perspectiveHref("/", {}, null), "/");
  assert.equal(perspectiveHref("/start-sit", { team_id: "2", week: "7" }, 3), "/start-sit?week=7&team=3");
  assert.equal(perspectiveHref("/x", new URLSearchParams("team=1&sort=a"), 2), "/x?sort=a&team=2");
});

test("picker labels mark the default team without claiming the viewer owns it", () => {
  assert.equal(teamOptionLabel({ name: "Blue", is_owner_default: true }), "Blue (default)");
  assert.equal(teamOptionLabel({ name: "Red", is_owner_default: false }), "Red");
  assert.equal(teamOptionLabel({ name: "Blue", is_mine: true }), "Blue (default)");
  assert.ok(!teamOptionLabel({ name: "Blue", is_mine: true }).includes("you"));
});

test("only a 404 about a requested team counts as an unknown team", () => {
  const sel = readTeamSelection({ team: "99" });
  assert.equal(isUnknownTeam({ ok: false, status: 404, error: "Team 99 is not in League (2026)." }, sel), true);
  assert.equal(isUnknownTeam({ ok: false, status: 503, error: "busy" }, sel), false);
  assert.equal(isUnknownTeam({ ok: false, status: 404, error: "Team 99 is not in League" }, { kind: "default" }), false);
  assert.equal(isUnknownTeam({ ok: true }, sel), false);
});

test("links carry the URL's team on team-scoped pages, and the last analyzed team from league-wide pages", () => {
  assert.equal(navTeam("/start-sit", "4", "7"), "4");                 // the URL wins on a team-scoped page
  assert.equal(navTeam("/start-sit", null, "7"), null);               // no team there is the explicit default
  assert.equal(navTeam("/rankings", null, "4"), "4");                 // Start/Sit for team 4 → Rankings → keeps team 4
  assert.equal(navTeam("/players", null, null), null);
  assert.equal(navTeam("/managers/3", null, "4"), "4");
  assert.equal(withTeam("/", navTeam("/rankings", null, "4")), "/?team=4");
  assert.equal(withTeam("/players?id=1", navTeam("/rankings", null, "4")), "/players?id=1");  // never leaks into league-wide pages
});

test("a stored last team must be a team id", () => {
  assert.equal(parseStoredTeam("4"), "4");
  assert.equal(parseStoredTeam(null), null);
  assert.equal(parseStoredTeam("4; drop"), null);
  assert.equal(parseStoredTeam(""), null);
});

test("Trade analyzer and Discovery links keep the analyzed team; links that name a team keep theirs", () => {
  assert.equal(withTeam("/trades?ta=4&tb=2&a=11&b=12", "4"), "/trades?ta=4&tb=2&a=11&b=12&team=4");
  assert.equal(withTeam("/trades/discover?mode=balanced", "4"), "/trades/discover?mode=balanced&team=4");
  assert.equal(withTeam("/luck-lab?team=2&vs=5", "4"), "/luck-lab?team=2&vs=5");
});

test("the Compare shortlist is team-scoped; the pair view and other /compare URLs stay league-wide (#175 review)", () => {
  assert.equal(isTeamScopedView("/compare", new URLSearchParams("ids=201,401")), true);
  assert.equal(isTeamScopedView("/compare", new URLSearchParams("ids=")), true);
  assert.equal(isTeamScopedView("/compare", { ids: "201,401", team: "1" }), true);
  assert.equal(isTeamScopedView("/compare/", new URLSearchParams("ids=1,2")), true);
  assert.equal(isTeamScopedView("/compare", new URLSearchParams("a=201&b=401")), false);
  assert.equal(isTeamScopedView("/compare", new URLSearchParams("a=201&b=401&ids=201,401")), false);   // a pair from a shortlist
  assert.equal(isTeamScopedView("/compare", new URLSearchParams("")), false);
  assert.equal(isTeamScopedView("/compare", null), false);
  assert.equal(isTeamScopedView("/rankings", new URLSearchParams("ids=1,2")), false);
  assert.equal(isTeamScopedView("/start-sit", null), true);
  assert.equal(isTeamScoped("/compare"), false);                    // the path alone is not
});

test("shortlist links carry the team; the rail's URL keeps the shortlist and drops the start pick", () => {
  assert.equal(withTeam("/compare?ids=201,401&slot=FLEX", "1"), "/compare?ids=201%2C401&slot=FLEX&team=1");
  assert.equal(withTeam("/compare?a=201&b=401", "1"), "/compare?a=201&b=401");
  assert.equal(withTeam("/compare", "1"), "/compare");
  // the rail picker and the page's "Analyzing" picker both build this URL (perspectiveHref, default reset)
  const sp = new URLSearchParams("ids=201,401,301&slot=FLEX&start=301&team=2");
  assert.equal(perspectiveHref("/compare", sp, 1), "/compare?ids=201%2C401%2C301&slot=FLEX&team=1");
  assert.equal(perspectiveHref("/compare", sp, null), "/compare?ids=201%2C401%2C301&slot=FLEX");
  // on the shortlist the URL is the truth for the rail, as on any team-scoped page: no team = the default
  const scoped = isTeamScopedView("/compare", new URLSearchParams("ids=1,2"));
  assert.equal(navTeam("/compare", null, "4", scoped), null);
  assert.equal(navTeam("/compare", "1", "4", scoped), "1");
  assert.equal(navTeam("/compare", null, "4", isTeamScopedView("/compare", new URLSearchParams("a=1&b=2"))), "4");
});

test("a league the visitor may not see is never read as a missing team", () => {
  // /v2 answers a non-member's request for a private league (and an unknown league) with this 404
  const hidden = { ok: false, status: 404, error: "League not found." };
  assert.equal(isTeamNotFound(hidden), false);
  assert.equal(isUnknownTeam(hidden, { kind: "team", id: 3 }), false);
  assert.equal(isUnknownTeam(hidden, { kind: "default" }), false);
  assert.equal(isTeamNotFound({ ok: false, status: 404, error: "Team 99 not found in this league." }), true);
  assert.equal(isTeamNotFound({ ok: false, status: 404, error: "Your team was not identified. Set ESPN_MY_TEAM_ID or pass team_id." }), true);
  assert.equal(isTeamNotFound({ ok: false, status: 503, error: "Team data unavailable" }), false);
  assert.equal(isTeamNotFound({ ok: true, status: 404, error: "team" }), false);
});
