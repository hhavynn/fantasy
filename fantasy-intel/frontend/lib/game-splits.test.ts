import assert from "node:assert/strict";
import { test } from "node:test";
import {
  formatDifference, formatSplit, parseSplitsState, reasonList, seasonOptions, splitsApiQuery, splitsHref, visibleWeeks,
  windowSentence,
} from "./game-splits.ts";
import type { SplitsWeek } from "./game-splits-types.ts";

test("URL state: valid ids, windows and dates are kept; anything malformed falls back", () => {
  const s = parseSplitsState({ player: "00-SPLWR01", teammate: "00-SPLWR02", season_from: "2025", week_from: "3",
    week_to: "12", as_of: "2025-10-01", show: "excluded" });
  assert.deepEqual(s, { player: "00-SPLWR01", teammate: "00-SPLWR02", seasonFrom: 2025, weekFrom: 3, seasonTo: null,
    weekTo: 12, asOf: "2025-10-01", show: "excluded" });
  const bad = parseSplitsState({ player: "<script>", teammate: "00-SPLWR02", week_from: "0", week_to: "19",
    as_of: "2025-02-30", season_from: "20x5", show: "pie" });
  assert.deepEqual(bad, { player: null, teammate: null, seasonFrom: null, weekFrom: null, seasonTo: null, weekTo: null,
    asOf: null, show: "all" });
  // a teammate equal to the player, or without a player, is dropped
  assert.equal(parseSplitsState({ player: "00-SPLWR01", teammate: "00-SPLWR01" }).teammate, null);
});

test("every input goes into the backend query; hrefs drop defaults and reset the teammate with the player", () => {
  const s = parseSplitsState({ player: "00-SPLWR01", teammate: "00-SPLWR02", season_from: "2025", as_of: "2025-10-01", show: "samples" });
  assert.equal(splitsApiQuery(s), "player=00-SPLWR01&teammate=00-SPLWR02&season_from=2025&as_of=2025-10-01");
  assert.equal(splitsHref(s), "/research/splits?player=00-SPLWR01&teammate=00-SPLWR02&season_from=2025&as_of=2025-10-01&show=samples");
  assert.equal(splitsHref(s, { player: "00-SPLTE01" }), "/research/splits?player=00-SPLTE01&season_from=2025&as_of=2025-10-01&show=samples");
  assert.equal(splitsHref(s, { show: "all", asOf: null }), "/research/splits?player=00-SPLWR01&teammate=00-SPLWR02&season_from=2025");
  assert.equal(splitsHref(parseSplitsState({})), "/research/splits");
});

test("formatting keeps missing values missing and shares in percentage points", () => {
  assert.equal(formatSplit(null, "num"), null);
  assert.equal(formatSplit(0, "num"), "0.0");               // a measured zero stays a zero
  assert.equal(formatSplit(0.4865, "pct"), "48.6%");
  assert.equal(formatSplit(-0.24, "signed"), "−0.2");
  assert.equal(formatDifference(0.2278, "pct"), "+22.8 pts");
  assert.equal(formatDifference(-2.8, "num"), "−2.8");
  assert.equal(formatDifference(undefined, "num"), null);
});

test("window sentence, reason lists, drilldown filter and season options", () => {
  assert.equal(windowSentence({ window: { start: { season: 2025, week: 1 }, end: { season: 2025, week: 18 }, as_of: null, weeks_listed: 18, usage_through_week: 12 } }),
    "2025 weeks 1–18");
  assert.equal(windowSentence({ window: { start: { season: 2024, week: 10 }, end: { season: 2025, week: 4 }, as_of: "2025-10-01", weeks_listed: 13, usage_through_week: 4 } }),
    "2024 week 10 to 2025 week 4, games played before 2025-10-01");
  assert.deepEqual(reasonList({ partial_game: 1, active_no_record: 2 }, { partial_game: "Partial game", active_no_record: "Active, no record" }),
    [["Active, no record", 2], ["Partial game", 1]]);
  const w = (outcome: SplitsWeek["outcome"]) => ({ outcome } as SplitsWeek);
  const weeks = [w("with"), w("excluded"), w("not_counted"), w("without")];
  assert.equal(visibleWeeks(weeks, "samples").length, 2);
  assert.equal(visibleWeeks(weeks, "excluded").length, 1);
  assert.equal(visibleWeeks(weeks, "all").length, 4);
  assert.deepEqual(seasonOptions([2024, 2025], 2023), [2025, 2024, 2023]);
});
