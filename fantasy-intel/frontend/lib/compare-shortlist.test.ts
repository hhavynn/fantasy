// The shortlist half of lib/compare.ts (issue #175): URL state, the cells (N/A and "—", never 0) and the start pick.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import {
  DASH, MAX_SHORTLIST, bestHeadline, compareHref, injuryCell, isBestProjected, isShortlistResponse, isShortlistView,
  metricCell, nextWeekCell, pairFromShortlistHref, parseCompareParams, parseShortlistParams, rosCell, rosterShort,
  rosterText, shortlistBackHref, shortlistFromPair, shortlistHref, shortlistPairs, startPickNote, trendCell,
  shortlistSourceHint, unknownShortlistId, withPlayer, withoutPlayer,
  VERDICT_STATES, verdictView,
  type CompareCard, type CompareResponse, type ShortlistResponse, type VerdictState,
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
      source: "espn", source_label: "ESPN", week: pair.as_of_week, slot: "FLEX", state: "incomplete_single", leaders: [11], value: 14.2, gap: 2.2, considered: [11, 12],
      excluded: [
        { espn_player_id: 13, reason: "not_eligible", text: "Charlie QB (QB) cannot start at FLEX." },
        { espn_player_id: 14, reason: "no_projection", text: "Delta TE has no ESPN projection." },
      ],
      text: "…",
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
  // the pair URL never carries the shortlist's team (#282 review): the pair view is league-wide, and a team= there
  // would pin the rail and every propagated link; the back link takes the analyzed team via TeamLink
  const href = pairFromShortlistHref(1, 3, { ids: [1, 2, 3], slot: "FLEX", start: 3, team: "4" });
  assert.equal(href, "/compare?a=1&b=3&ids=1,2,3&slot=FLEX&start=3");
  assert.doesNotMatch(href, /team=/);
  const sp = Object.fromEntries(new URL(href, "http://x").searchParams);
  assert.equal(isShortlistView(sp), false);
  assert.deepEqual(parseCompareParams(sp), { a: 1, b: 3, errors: [] });
  assert.equal(shortlistBackHref(sp), "/compare?ids=1,2,3&slot=FLEX&start=3");
  assert.equal(shortlistBackHref({ ...sp, team: "4" }), "/compare?ids=1,2,3&slot=FLEX&start=3");    // an old link's team is not reused
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

test("the payload guard requires a known verdict state", () => {
  const r = shortlist();
  assert.ok(isShortlistResponse(r));
  assert.equal(isShortlistResponse({ players: [] }), false);
  assert.equal(isShortlistResponse({ ...r, best_projected: { ...r.best_projected, state: "leader" } }), false);
  assert.equal(isBestProjected(r, 11), true);
  assert.equal(isBestProjected(r, 13), false);
});

/** The fixture's verdict in one state: Alpha WR 14.2 and Bravo RB 12.0 are FLEX-eligible and projected, Charlie QB
 *  cannot start at FLEX, Delta TE has no projection. `slot: null` makes the "Any" cases. */
function inState(state: VerdictState, over: Partial<ShortlistResponse["best_projected"]> = {}, slot: string | null = "FLEX"): ShortlistResponse {
  const r = shortlist();
  r.slot = slot;
  if (slot === null) for (const p of r.players) p.slot_eligible = null;
  const ranked = ["single_leader", "tied", "only", "incomplete_single", "incomplete_tied"].includes(state);
  r.best_projected = {
    ...r.best_projected, state, slot, leaders: ranked ? [11] : [], value: ranked ? 14.2 : null, gap: ranked ? 2.2 : null,
    considered: ranked ? [11, 12] : [], excluded: ranked ? r.best_projected.excluded : [], ...over,
  };
  return r;
}

// Review round 3: one row per state, with everything the page says for it. The page renders from `state` alone.
const VERDICTS: {
  state: VerdictState; over?: Partial<ShortlistResponse["best_projected"]>; slot?: string | null;
  headline: string; tone: "info" | "caution"; bestNote: string; compared: [string, string];
  pickAlpha: string; pickBravo: string;
}[] = [
  {
    state: "single_leader", headline: "Alpha WR is best projected at FLEX for week 3", tone: "info", bestNote: "Alpha WR",
    compared: ["2 of 4", "2 left out, reasons below"],
    pickAlpha: "Your pick, Alpha WR, is also the best projected (ESPN).",
    pickBravo: "Your pick, Bravo RB, is projected 2.2 points below the best projected (ESPN).",
  },
  {
    state: "only", over: { considered: [11], gap: null }, headline: "Alpha WR is the only player at FLEX with a projection for week 3",
    tone: "info", bestNote: "Alpha WR", compared: ["1 of 4", "2 left out, reasons below"],
    pickAlpha: "Your pick, Alpha WR, is also the best projected (ESPN).",
    pickBravo: "Your pick, Bravo RB, is projected 2.2 points below the best projected (ESPN).",
  },
  {
    state: "tied", over: { leaders: [11, 12] }, headline: "Alpha WR and Bravo RB are tied for best projected at FLEX", tone: "info",
    bestNote: "Tied", compared: ["2 of 4", "2 left out, reasons below"],
    pickAlpha: "Your pick, Alpha WR, is among the players tied for the best ESPN projection.",
    pickBravo: "Your pick, Bravo RB, is among the players tied for the best ESPN projection.",
  },
  {
    state: "incomplete_single", headline: "Alpha WR is best projected at FLEX of the players with a projection", tone: "caution",
    bestNote: "Alpha WR", compared: ["2 of 4", "2 left out, reasons below"],
    pickAlpha: "Your pick, Alpha WR, is also the best projected (ESPN) of the players with a projection.",
    pickBravo: "Your pick, Bravo RB, is projected 2.2 points below the best projected (ESPN) of the players with a projection.",
  },
  {
    // Codex round 3: tied leaders with a missing projection keep the qualification
    state: "incomplete_tied", over: { leaders: [11, 12] },
    headline: "Alpha WR and Bravo RB are tied for best projected at FLEX of the players with a projection", tone: "caution",
    bestNote: "Tied", compared: ["2 of 4", "2 left out, reasons below"],
    pickAlpha: "Your pick, Alpha WR, is among the players tied for the best ESPN projection of the players with a projection.",
    pickBravo: "Your pick, Bravo RB, is among the players tied for the best ESPN projection of the players with a projection.",
  },
  {
    state: "no_common_slot", slot: null, headline: "These players never share a lineup slot: pick a slot to compare them", tone: "caution",
    bestNote: "None", compared: [DASH, "Pick a slot: they never share one"],
    pickAlpha: "Your pick, Alpha WR, is projected 14.2 (ESPN). These players never share a lineup slot, so no one is best projected to compare with; pick a slot.",
    pickBravo: "Your pick, Bravo RB, is projected 12.0 (ESPN). These players never share a lineup slot, so no one is best projected to compare with; pick a slot.",
  },
  {
    // Codex round 3: unsynced slots are not "never share", and the page never asks for a slot it cannot offer
    state: "slots_unknown", slot: null, headline: "Not ranked: this league's lineup slots are not synced", tone: "caution",
    bestNote: "None", compared: [DASH, "Lineup slots not synced"],
    pickAlpha: "Your pick, Alpha WR, is projected 14.2 (ESPN). This league's lineup slots are not synced, so there is no ranking to compare with.",
    pickBravo: "Your pick, Bravo RB, is projected 12.0 (ESPN). This league's lineup slots are not synced, so there is no ranking to compare with.",
  },
  {
    state: "none_eligible", headline: "No player here can fill FLEX", tone: "caution", bestNote: "None", compared: ["0 of 4", "All of them"],
    pickAlpha: "Your pick, Alpha WR, is projected 14.2 (ESPN). No player who can fill the slot is best projected to compare with.",
    pickBravo: "Your pick, Bravo RB, is projected 12.0 (ESPN). No player who can fill the slot is best projected to compare with.",
  },
  {
    state: "none_projected", headline: "No best projected at FLEX for week 3", tone: "caution", bestNote: "None", compared: ["0 of 4", "All of them"],
    pickAlpha: "Your pick, Alpha WR, is projected 14.2 (ESPN). No player who can fill the slot is best projected to compare with.",
    pickBravo: "Your pick, Bravo RB, is projected 12.0 (ESPN). No player who can fill the slot is best projected to compare with.",
  },
];

test("the verdict table covers every state", () => {
  assert.deepEqual(VERDICTS.map((v) => v.state).sort(), [...VERDICT_STATES].sort());
});

for (const v of VERDICTS) {
  test(`verdict state ${v.state}: headline, tone, figures and start pick`, () => {
    const r = inState(v.state, v.over, v.slot === undefined ? "FLEX" : v.slot);
    r.as_of_week = 3;
    r.best_projected.week = 3;
    const view = verdictView(r);
    assert.equal(view.headline, v.headline);
    assert.equal(bestHeadline(r), v.headline);
    assert.equal(view.tone, v.tone);
    assert.equal(view.best.note, v.bestNote);
    assert.deepEqual([view.compared.value, view.compared.note], v.compared);
    assert.doesNotMatch(view.headline, /\bstart\b/i);                   // never a start verdict
    assert.equal(startPickNote(r, 11), v.pickAlpha);
    assert.equal(startPickNote(r, 12), v.pickBravo);
    // never "no projection" for a pick that has one; only Delta TE (missing) says so
    assert.equal(startPickNote(r, 14), "Your pick, Delta TE, has no ESPN projection to compare with.");
    if (v.slot !== null) assert.equal(startPickNote(r, 13), "Your pick, Charlie QB, cannot start at FLEX.");
  });
}

test("start pick basics: none, unknown and an empty source", () => {
  const r = shortlist();
  assert.equal(startPickNote(r, null), null);
  assert.equal(startPickNote(r, 99), null);
  r.best_projected.source_label = "Unavailable";            // no player has a number: no source is named
  r.best_projected.value = null;
  assert.equal(startPickNote(r, 14), "Your pick, Delta TE, has no projection to compare with.");
});

test("row source hints name a source only when the row has its numbers (#175 review L2)", () => {
  const r = shortlist();
  assert.equal(shortlistSourceHint(r, "next"), "ESPN");
  assert.equal(shortlistSourceHint(r, "ros"), r.players.some((p) => p.card.projection?.ros?.espn != null) ? "ESPN" : "Unavailable");
  // everyone on bye or unprojected this week: the week row is Unavailable, the ROS row still names ESPN for its totals
  r.best_projected.source_label = "Unavailable";
  for (const p of r.players) p.card.projection!.ros!.espn = 120.5;
  assert.equal(shortlistSourceHint(r, "next"), "Unavailable");
  assert.equal(shortlistSourceHint(r, "ros"), "ESPN");
  for (const p of r.players) p.card.projection!.ros!.espn = null;
  assert.equal(shortlistSourceHint(r, "ros"), "Unavailable");
  r.best_projected.source = "ours";
  r.best_projected.source_label = "Fantasy Intel";
  r.players[1].card.projection!.ros!.ours = 98.2;
  assert.equal(shortlistSourceHint(r, "next"), "Ours");
  assert.equal(shortlistSourceHint(r, "ros"), "Ours");
});
