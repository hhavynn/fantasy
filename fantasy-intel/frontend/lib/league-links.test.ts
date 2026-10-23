// Links, team-picker navigations and no-JS form actions rendered on a league page (`/l/<lg>/...`) stay in that league:
// `leagueLinkHref` (components/LeagueLink.tsx, TeamLink, the router pushes) and `withLeague` (the server-scoped form
// actions), checked end to end against proxy.ts's routing with a last-viewed-league cookie that names another league.
import assert from "node:assert/strict";
import test from "node:test";
import { compareHref } from "./compare.ts";
import { leagueLinkHref, proxyDecision, withLeague } from "./league-scope.ts";
import { parseResearchState, researchHref } from "./research.ts";
import { perspectiveHref, withTeam } from "./team-perspective.ts";
import { parseRecapQuery, recapHref } from "./weekly-recap.ts";

const LG = "lg_" + "a".repeat(24);
const OTHER = "lg_" + "z".repeat(24);   // the league another tab viewed last (the fi_league cookie)
const PAGE = `/l/${LG}/rankings`;        // usePathname() on a league page
const CLASSIC = "/rankings";             // usePathname() on the classic site

/** proxy.ts's answer for a browser request to `href`, with the cookie naming the other league. */
function served(href: string) {
  const url = new URL(href, "http://x");
  return proxyDecision({ enabled: true, pathname: url.pathname, search: url.search, cookie: OTHER });
}

/** On a league page `href` is served for that league whatever the cookie says; the bare href (before this change)
 *  followed the cookie to the other league; and the classic site keeps it exactly as built. */
function assertStaysInLeague(href: string, scoped: string, classic: string) {
  const [path] = href.split("#");
  assert.equal(scoped, `/l/${LG}${href}`, href);
  assert.deepEqual(served(scoped), { kind: "rewrite", path, league: LG }, href);
  assert.deepEqual(served(href), { kind: "redirect", location: `/l/${OTHER}${path}` }, href);
  assert.equal(classic, href, href);
}

test("shared links and pushed hrefs rendered on a league page stay in that league end to end", () => {
  const research = parseResearchState({ window: "last4", team: "KC" });
  const recap = parseRecapQuery({ week: "6" });
  const hrefs = [
    "/players?id=201",                                         // PlayerIdentity, PlayerLink
    compareHref(201, 305), compareHref(201),                   // CompareLink
    "/data",                                                   // ProvenanceBadge, SyncPill
    recapHref(recap, { week: 5 }), researchHref("wr", research, { view: "momentum" }),  // Tabs items
    researchHref("wr", research, { sel: "00-0036322" }),        // PlayerPicker, InteractiveScatter router.push
    `${researchHref("wr", research, { sel: "00-0036322" })}#evidence`,   // InteractiveScatter "View evidence"
    perspectiveHref("/start-sit", { week: "7", team: "1" }, 4), // TeamPerspective option (TeamPerspectiveSelect push)
    perspectiveHref("/start-sit", { week: "7", team: "4" }, null),   // TeamPerspective "Back to default"
  ];
  for (const href of hrefs) assertStaysInLeague(href, leagueLinkHref(href, PAGE), leagueLinkHref(href, CLASSIC));
  // The team picker keeps the chosen team and the page's other params inside the league.
  const picked = new URL(leagueLinkHref(perspectiveHref("/start-sit", { week: "7", team: "1" }, 4), PAGE), "http://x");
  assert.equal(picked.pathname, `/l/${LG}/start-sit`);
  assert.equal(picked.searchParams.get("team"), "4");
  assert.equal(picked.searchParams.get("week"), "7");
});

test("TeamLink keeps the team and falls back to the page's league when no league prop is passed", () => {
  const href = withTeam("/trades/discover", "4");
  assertStaysInLeague(href, leagueLinkHref(href, PAGE), leagueLinkHref(href, CLASSIC));
  assert.equal(new URL(leagueLinkHref(href, PAGE), "http://x").searchParams.get("team"), "4");
  // An explicit league prop (defenses, kickers pass currentLeague()) still decides; null falls back to the page.
  assert.equal(leagueLinkHref("/schedule-intelligence#kdst-streaming", PAGE, LG), `/l/${LG}/schedule-intelligence#kdst-streaming`);
  assert.equal(leagueLinkHref("/schedule-intelligence", PAGE, null), `/l/${LG}/schedule-intelligence`);
  assert.equal(leagueLinkHref("/schedule-intelligence", CLASSIC, null), "/schedule-intelligence");
});

test("no-JS form actions scoped on the server stay in the league; the classic site keeps the bare path", () => {
  const actions = ["/defenses", "/kickers", "/movers", "/players", "/playoff-race", "/rankings", "/schedule-intelligence", "/research/wr"];
  for (const action of actions) {
    // `withLeague(action, (await currentLeague())?.id)`: the id on a league page, undefined on the classic site.
    assertStaysInLeague(action, withLeague(action, LG), withLeague(action, undefined));
    // A GET form replaces the action's query with its fields, so the league must be in the path, never the query.
    const submitted = new URL(withLeague(action, LG), "http://x");
    submitted.search = "?week=7&q=lloyd";
    assert.deepEqual(served(submitted.pathname + submitted.search), { kind: "rewrite", path: `${action}?week=7&q=lloyd`, league: LG });
  }
});

test("external, relative, anchor, unscoped and already-scoped hrefs pass through unchanged", () => {
  const untouched = [
    "https://fantasy.espn.com/football/team", "//evil.example/x", "mailto:a@b.c",   // external
    "?sort=rank&dir=asc", "?id=201&tab=usage", "#evidence", "",                     // relative: already in the page's league
    "/leagues", "/leagues/demo", "/login?next=/leagues", "/signup?next=%2Fleagues", "/account", "/terms", "/privacy",   // belong to no league
    "/?league=clear",                                                               // the way back to the classic site
    `/l/${OTHER}/players?id=1`, `/l/${LG}/data`,                                    // already scoped (switcher, Nav)
    "/api/search?q=a", "/_next/static/chunk.js", "/icon.png",                       // route handlers and assets
  ];
  for (const href of untouched) {
    assert.equal(leagueLinkHref(href, PAGE), href, href);
    assert.equal(leagueLinkHref(href, CLASSIC), href, href);
  }
});

test("a malformed or missing page path never invents a league", () => {
  assert.equal(leagueLinkHref("/players?id=1", "/l/lg_short/rankings"), "/players?id=1");
  assert.equal(leagueLinkHref("/players?id=1", null), "/players?id=1");
  assert.equal(leagueLinkHref("/players?id=1", undefined), "/players?id=1");
  assert.equal(leagueLinkHref("/players?id=1", CLASSIC, "lg_bad"), "/players?id=1");
});
