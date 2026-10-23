import assert from "node:assert/strict";
import { test } from "node:test";
import {
  DEFAULT_TAB, PLAYER_TABS, inRange, parsePlayerTab, parseWeekWindow, profileHref, profileTabItems, readProfileState,
  weekSpan, weekWindowLabel, windowRange,
} from "./player-tabs.ts";

test("parsePlayerTab: known tabs only, anything else is the Overview", () => {
  for (const t of PLAYER_TABS) assert.equal(parsePlayerTab(t.id), t.id);
  for (const raw of [undefined, "", "news", "USAGE", "game_log", "../usage"]) assert.equal(parsePlayerTab(raw), DEFAULT_TAB);
  assert.equal(parsePlayerTab(["outlook", "usage"]), "outlook");
});

test("there is no News tab: no free news source with redistribution rights is wired in", () => {
  assert.deepEqual(PLAYER_TABS.map((t) => t.id), ["overview", "usage", "game-log", "matchups", "outlook"]);
});

test("parseWeekWindow: only the offered windows; anything else is the season", () => {
  assert.equal(parseWeekWindow("3"), 3);
  assert.equal(parseWeekWindow("5"), 5);
  assert.equal(parseWeekWindow("8"), 8);
  for (const raw of [undefined, "", "4", "0", "-3", "3.0", "all"]) assert.equal(parseWeekWindow(raw), null);
  assert.equal(weekWindowLabel(null), "Season");
  assert.equal(weekWindowLabel(5), "Last 5 weeks");
});

test("readProfileState: needs a numeric id and normalises the rest", () => {
  assert.equal(readProfileState({}), null);
  assert.equal(readProfileState({ id: "abc" }), null);
  assert.deepEqual(readProfileState({ id: "201" }), { id: "201", tab: "overview", weeks: null, cols: "core", q: "", team: null });
  assert.deepEqual(
    readProfileState({ id: "-16001", tab: "usage", weeks: "5", cols: "all", q: " echo ", team: "4" }),
    { id: "-16001", tab: "usage", weeks: 5, cols: "all", q: "echo", team: "4" },
  );
  assert.equal(readProfileState({ id: "201", team: "abc" })?.team, null);
});

test("profileHref: relative query strings, defaults left out, so /players?id= stays the Overview URL", () => {
  const s = readProfileState({ id: "201" })!;
  assert.equal(profileHref(s), "?id=201");
  assert.equal(profileHref(s, { tab: "overview" }), "?id=201");
  assert.equal(profileHref(s, { tab: "usage" }), "?id=201&tab=usage");
  assert.equal(profileHref(s, { tab: "game-log", weeks: 3, cols: "all" }), "?id=201&tab=game-log&weeks=3&cols=all");
  // A relative href keeps the path, so /l/<lg>/players stays in its league.
  for (const item of profileTabItems(s)) assert.ok(item.href.startsWith("?id=201"), item.href);
});

test("profileHref: switching tabs keeps the week window, columns, search text and team", () => {
  const s = readProfileState({ id: "201", tab: "usage", weeks: "5", cols: "all", q: "a&b", team: "4" })!;
  assert.equal(profileHref(s, { tab: "outlook" }), "?id=201&tab=outlook&weeks=5&cols=all&q=a%26b&team=4");
  assert.equal(profileHref(s, { weeks: null }), "?id=201&tab=usage&cols=all&q=a%26b&team=4");
});

test("windowRange: the last n calendar weeks, byes included as gaps", () => {
  assert.equal(windowRange([1, 2, 3], 7, null), null);
  assert.deepEqual(windowRange([1, 2, 4, 5, 7], 7, 3), { from: 5, to: 7 });
  // A record past the last completed week extends the axis, as the charts do.
  assert.deepEqual(windowRange([1, 9], 7, 5), { from: 5, to: 9 });
  // A window longer than the season starts at week 1.
  assert.deepEqual(windowRange([1, 2], 2, 8), { from: 1, to: 2 });
  assert.equal(windowRange([], null, 3), null);
});

test("inRange and weekSpan", () => {
  assert.equal(inRange(4, null), true);
  assert.equal(inRange(4, { from: 5, to: 7 }), false);
  assert.equal(inRange(6, { from: 5, to: 7 }), true);
  assert.equal(weekSpan([]), null);
  assert.equal(weekSpan([5]), "wk 5");
  assert.equal(weekSpan([7, 3, 5]), "wk 3–7");
});
