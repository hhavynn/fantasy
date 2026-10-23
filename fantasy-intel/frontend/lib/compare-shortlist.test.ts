// The shortlist half of lib/compare.ts (issue #175): URL state, the cells (N/A and "—", never 0) and the start pick.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import {
  DASH, MAX_SHORTLIST, bestHeadline, compareHref, injuryCell, isBestProjected, isShortlistResponse, isShortlistView,
  metricCell, nextWeekCell, pairFromShortlistHref, parseCompareParams, parseShortlistParams, rosCell, rosterShort,
  rosterText, shortlistBackHref, shortlistFromPair, shortlistHref, shortlistPairs, startPickNote, trendCell,
  unknownShortlistId, withPlayer, withoutPlayer,
  type CompareCard, type CompareResponse, type ShortlistResponse,
} from "./compare.ts";

const pair = JSON.parse(readFileSync(new URL("./fixtures/compare.synthetic.json", import.meta.url), "utf8")) as CompareResponse;

function withId(card: CompareCard, id: number, name: string, position: string, espn: number | null): CompareCard {
  const c = structuredClone(card);
  c.player.espn_player_id = id;
  c.player.full_name = name;
  c.player.position = position;
  c.projection!.next_week!.espn = espn;
  c.projection!.next_week!.on_bye = false;
  return c;
}

/** Four synthetic players for a FLEX slot: Alpha WR 14.2 (best), Bravo RB 12.0, Charlie QB 20.0 (not eligible), Delta TE no projection. */
function shortlist(): ShortlistResponse {
  const [a, b] = pair.players;
  const players = [
    { card: withId(a, 11, "Alpha WR", "WR", 14.2), roster: "this_team" as const, eligible_slots: ["WR", "FLEX"], slot_eligible: true },
    { card: withId(b, 12, "Bravo RB", "RB", 12.0), roster: "free_agent" as const, eligible_slots: ["RB", "FLEX"], slot_eligible: true },
    { card: withId(a, 13, "Charlie QB", "QB", 20.0), roster: "other_team" as const, eligible_slots: ["QB"], slot_eligible: false },
    { card: withId(b, 14, "Delta TE", "TE", null), roster: "waivers" as const, eligible_slots: ["TE", "FLEX"], slot_eligible: true },
  ];
  return {
    as_of_week: pair.as_of_week, completed_through_week: pair.completed_through_week, final_week: pair.final_week,
    source: "espn", provider: "espn", scoring: pair.scoring,
    slots: [{ slot: "WR", count: 2, positions: ["WR"] }, { slot: "FLEX", count: 1, positions: ["RB", "WR", "TE"] }],
    unsupported_slots: [], slot: "FLEX", team: { team_id: 3, name: "Team Three", is_owner_default: true }, players,
    best_projected: {
      source: "espn", week: pair.as_of_week, slot: "FLEX", leaders: [11], value: 14.2, gap: 2.2, considered: [11, 12],
      excluded: [
        { espn_player_id: 13, reason: "not_eligible", text: "Charlie QB (QB) cannot start at FLEX." },
        { espn_player_id: 14, reason: "no_projection", text: "Delta TE has no ESPN projection." },
      ],
      complete: false, status: "incomplete", text: "…",
    },
    limitations: [], disclaimer: "…", freshness: { espn_synced_at: null, nfl_synced_at: null },
  };
}

// ------------------------------------------------------------------------------------- URL state --

test("a/b URLs keep opening the pair view; ids alone opens the shortlist", () => {
  assert.equal(isShortlistView({ a: "1", b: "2" }), false);
  assert.equal(isShortlistView({ a: "1", b: "2", ids: "1,2,3" }), false);      // a pair opened from a shortlist
  assert.equal(isShortlistView({ ids: "" }), true);
  assert.equal(isShortlistView({ ids: "1,2,3" }), true);
  assert.equal(isShortlistView({}), false);
  assert.deepEqual(parseCompareParams({ a: "1", b: "2", ids: "1,2,3" }), { a: 1, b: 2, errors: [] });
  assert.equal(compareHref(1, 2), "/compare?a=1&b=2");
});

test("parseShortlistParams keeps valid ids in order, drops repeats and caps at four", () => {
  assert.deepEqual(parseShortlistParams({ ids: "5, -16001,5,7" }), { ids: [5, -16001, 7], slot: null, start: null, errors: [] });
  const many = parseShortlistParams({ ids: "1,2,3,4,5,6" });
  assert.deepEqual(many.ids, [1, 2, 3, 4]);
  assert.equal(many.errors.length, 1);
  const junk = parseShortlistParams({ ids: "1,abc,2" });
  assert.deepEqual(junk.ids, [1, 2]);
  assert.match(junk.errors[0], /“abc” is not a player id/);
  assert.deepEqual(parseShortlistParams({ ids: ["1", "2"] }).ids, [1, 2]);
});

test("parseShortlistParams reads the slot and a start pick only when it is on the list", () => {
  assert.equal(parseShortlistParams({ ids: "1,2", slot: "flex" }).slot, "FLEX");
  assert.equal(parseShortlistParams({ ids: "1,2", slot: "D/ST" }).slot, "D/ST");
  const bad = parseShortlistParams({ ids: "1,2", slot: "LB" });
  assert.equal(bad.slot, null);
  assert.equal(bad.errors.length, 1);
  assert.equal(parseShortlistParams({ ids: "1,2", start: "2" }).start, 2);
  assert.equal(parseShortlistParams({ ids: "1,2", start: "9" }).start, null);
});

test("shortlistHref round-trips and stays in the shortlist view", () => {
  const href = shortlistHref({ ids: [1, -16001, 3], slot: "D/ST", start: 3, team: 4 });
  assert.equal(href, "/compare?ids=1,-16001,3&slot=D%2FST&start=3&team=4");
  const sp = Object.fromEntries(new URL(href, "http://x").searchParams);
  assert.equal(isShortlistView(sp), true);
  assert.deepEqual(parseShortlistParams(sp), { ids: [1, -16001, 3], slot: "D/ST", start: 3, errors: [] });
  assert.equal(shortlistHref({ ids: [] }), "/compare?ids=");
  assert.equal(shortlistHref({ ids: [1, 2], start: 9 }), "/compare?ids=1,2");             // a pick off the list is dropped
  assert.equal(shortlistHref({ ids: [1, 2, 3, 4, 5] }), "/compare?ids=1,2,3,4");
});

test("adding and removing players", () => {
  assert.deepEqual(withPlayer([1, 2], 3), [1, 2, 3]);
  assert.deepEqual(withPlayer([1, 2], 2), [1, 2]);
  assert.deepEqual(withPlayer([1, 2, 3, 4], 5), [1, 2, 3, 4]);
  assert.equal(MAX_SHORTLIST, 4);
  assert.deepEqual(withoutPlayer([1, 2, 3], 2), [1, 3]);
});

test("every pair opens the existing two-player view, with the way back", () => {
  assert.deepEqual(shortlistPairs([1, 2, 3]), [[1, 2], [1, 3], [2, 3]]);
  assert.equal(shortlistPairs([1, 2, 3, 4]).length, 6);
  const href = pairFromShortlistHref(1, 3, { ids: [1, 2, 3], slot: "FLEX", team: "4" });
  assert.equal(href, "/compare?a=1&b=3&ids=1,2,3&slot=FLEX&team=4");
  const sp = Object.fromEntries(new URL(href, "http://x").searchParams);
  assert.equal(isShortlistView(sp), false);
  assert.deepEqual(parseCompareParams(sp), { a: 1, b: 3, errors: [] });
  assert.equal(shortlistBackHref(sp), "/compare?ids=1,2,3&slot=FLEX&team=4");
  assert.equal(shortlistBackHref({ a: "1", b: "2" }), null);
  assert.equal(shortlistFromPair(1, 2), "/compare?ids=1,2");
  assert.equal(shortlistFromPair(null, undefined), "/compare?ids=");
});

test("unknownShortlistId reads the backend's 404", () => {
  assert.equal(unknownShortlistId("Unknown ESPN player id 999 (player 3)"), 999);
  assert.equal(unknownShortlistId("Unknown ESPN player id -16001 (player 1)"), -16001);
  assert.equal(unknownShortlistId("Team 9 is not in L"), null);
});

// ----------------------------------------------------------------------------------------- cells --

test("missing and not-applicable cells are a dash or N/A, never 0", () => {
  const r = shortlist();
  const delta = r.players[3].card;
  assert.deepEqual(nextWeekCell(delta, "espn"), { text: DASH, kind: "missing", why: `No ESPN projection for week ${r.as_of_week}.` });
  const qb = structuredClone(r.players[2].card);
  qb.usage.metrics.target_share = { value: null, applicable: false, why: "Receiving usage is not a QB role measure." };
  assert.deepEqual(metricCell(qb, "target_share", "share"), { text: "N/A", kind: "na", why: "Receiving usage is not a QB role measure." });
  qb.usage.metrics.snap_share = { value: null, applicable: true, why: "No usage record" };
  assert.equal(metricCell(qb, "snap_share", "share").text, DASH);
  qb.usage.metrics.snap_share = { value: 0, applicable: true, why: null };
  assert.equal(metricCell(qb, "snap_share", "share").text, "0.0%");                    // a measured zero stays 0
  assert.equal(metricCell(qb, "nope", "share").kind, "missing");
  const bye = structuredClone(delta);
  bye.projection!.next_week!.on_bye = true;
  assert.equal(nextWeekCell(bye, "espn").text, "Bye");
  const none = structuredClone(delta);
  none.projection = null;
  assert.equal(nextWeekCell(none, "espn").kind, "missing");
  assert.equal(rosCell(none, "espn").kind, "missing");
  assert.equal(trendCell(null, "Usage trend").text, DASH);
  assert.equal(trendCell("INSUFFICIENT", "Usage trend").text, DASH);
  assert.equal(trendCell("UP", "Usage trend").text, "Up");
});

test("injury: no designation is said as such, never as healthy", () => {
  const card = structuredClone(shortlist().players[0].card);
  card.availability.has_designation = false;
  assert.equal(injuryCell(card).text, "None reported");
  card.availability.has_designation = true;
  card.availability.label = "Questionable (ESPN and NFL report, week 6)";
  assert.equal(injuryCell(card).text, "Questionable");
});

test("roster status reads relative to the selected team", () => {
  const r = shortlist();
  assert.equal(rosterText(r.players[0], "Team Three"), "On Team Three");
  assert.equal(rosterText(r.players[1], null), "Free agent");
  assert.equal(rosterText(r.players[3], null), "On waivers");
  assert.equal(rosterShort("this_team"), "Yours");
  assert.equal(rosterShort("unknown"), DASH);
});

// ---------------------------------------------------------------------------- best projected --

test("the headline names who is best projected, never who to start", () => {
  const r = shortlist();
  assert.ok(isShortlistResponse(r));
  assert.equal(isShortlistResponse({ players: [] }), false);
  assert.equal(isBestProjected(r, 11), true);
  assert.equal(isBestProjected(r, 13), false);
  assert.equal(bestHeadline(r), "Alpha WR is best projected at FLEX of the players with a projection");
  r.best_projected.status = "leader";
  assert.equal(bestHeadline(r), `Alpha WR is best projected at FLEX for week ${r.as_of_week}`);
  r.best_projected.leaders = [11, 12];
  assert.equal(bestHeadline(r), "Alpha WR and Bravo RB are tied for best projected at FLEX");
  r.best_projected.leaders = [];
  r.best_projected.status = "none";
  assert.doesNotMatch(bestHeadline(r), /start/i);
});

test("the start pick is the viewer's, compared with best projected from the backend's numbers only", () => {
  const r = shortlist();
  assert.equal(startPickNote(r, null), null);
  assert.equal(startPickNote(r, 11), "Your pick, Alpha WR, is also the best projected (ESPN).");
  assert.equal(startPickNote(r, 12), "Your pick, Bravo RB, is projected 2.2 points below the best projected (ESPN).");
  assert.equal(startPickNote(r, 13), "Your pick, Charlie QB, cannot start at FLEX.");
  assert.equal(startPickNote(r, 14), "Your pick, Delta TE, has no ESPN projection to compare with.");
  assert.equal(startPickNote(r, 99), null);
});
