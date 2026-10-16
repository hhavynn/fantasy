import assert from "node:assert/strict";
import { test } from "node:test";
import {
  apiQuery, formatMetric, heatBucket, hiddenSentence, keyIds, parseResearchState, researchHref, shortName, sortValue,
  stateParams, symmetricDomain, tradeHref, waiverHref, weekList, zeroDomain,
} from "./research.ts";
import type { ResearchPlayer } from "./research-types.ts";
import { sortRows } from "./sort.ts";

function player(id: string, over: Partial<ResearchPlayer["window"]> = {}, delta: number | null = null): ResearchPlayer {
  return {
    gsis_id: id, name: `Player ${id}`, position: "WR", team: "KC", espn_player_id: null, mapping_status: "unmapped",
    games_played: 4, excluded_games: null, last_game_week: 4, market: null, injury: null,
    group: { key: "high_inline", label: "" }, research_groups: [],
    window: { games: 4, weeks: [1, 2, 3, 4], coverage: {}, ...over },
    momentum: { xfp_delta: delta, ppr_delta: null, xfp_recent: null, xfp_baseline: null, ppr_recent: null, ppr_baseline: null,
      recent_weeks: [], baseline_weeks: [], label: "FLAT", reading: null,
      confidence: { level: "LOW", label: "", n_recent: 2, n_baseline: 2 }, metrics: {}, volume_metric: "targets", share_metric: "target_share" },
  };
}

test("URL state: defaults, validation and clamping", () => {
  const d = parseResearchState({});
  assert.deepEqual([d.window, d.minGames, d.minVolume, d.view, d.y, d.sel], ["season", null, null, "opportunity", "fpoe", null]);
  const s = parseResearchState({ window: "last3", games: "5", vol: "7", team: "kc", view: "momentum", y: "ppr", sel: "00-0036900", cmp: "00-0036900" });
  assert.equal(s.minGames, 3);                     // clamped to the window size
  assert.equal(s.team, "KC");
  assert.equal(s.cmp, null);                       // comparing a player with themself is dropped
  const bad = parseResearchState({ window: "last9", games: "x", vol: "-1", team: "K1", view: "pie", sel: "<script>" });
  assert.deepEqual([bad.window, bad.minGames, bad.minVolume, bad.team, bad.view, bad.sel], ["season", null, null, null, "opportunity", null]);
});

test("hrefs keep filters when switching views and drop defaults", () => {
  const s = parseResearchState({ window: "last5", vol: "7", sel: "00-0036900" });
  assert.equal(researchHref("wr", s, { view: "workload" }), "/research/wr?window=last5&vol=7&view=workload&sel=00-0036900");
  assert.equal(researchHref("rb", parseResearchState({})), "/research/rb");
  assert.equal(apiQuery("TE", s), "position=TE&window=last5&min_volume=7");
  assert.deepEqual(stateParams({ ...s, sort: "xfp_pg", dir: "asc" }), { window: "last5", vol: "7", sel: "00-0036900" });
});

test("missing values format as null, not zero", () => {
  assert.equal(formatMetric(null, "pts"), null);
  assert.equal(formatMetric(undefined, "pct"), null);
  assert.equal(formatMetric(0, "pct"), "0.0%");
  assert.equal(formatMetric(-2.04, "signed"), "−2.0");
  assert.equal(formatMetric(0.04, "signed"), "0.0");
});

test("heat buckets skip missing values", () => {
  assert.equal(heatBucket(undefined), null);
  assert.equal(heatBucket(null), null);
  assert.deepEqual([heatBucket(95), heatBucket(80), heatBucket(50), heatBucket(10)], [4, 3, 2, 1]);
});

test("sorting null metrics keeps them last in both directions", () => {
  const rows = [player("a", { target_share: null }), player("b", { target_share: 0.2 }), player("c", { target_share: 0.1 })];
  assert.deepEqual(sortRows(rows, (p) => sortValue(p, "target_share"), "desc").map((p) => p.gsis_id), ["b", "c", "a"]);
  assert.deepEqual(sortRows(rows, (p) => sortValue(p, "target_share"), "asc").map((p) => p.gsis_id), ["c", "b", "a"]);
  const m = [player("x", {}, null), player("y", {}, 3), player("z", {}, -1)];
  assert.deepEqual(sortRows(m, (p) => sortValue(p, "momentum"), "asc").map((p) => p.gsis_id), ["z", "y", "x"]);
});

test("key labels come from the data, not names", () => {
  const rows = [player("a", { xfp_pg: 10 }), player("b", { xfp_pg: 20 }), player("c", { xfp_pg: null }), player("d", { xfp_pg: 15 })];
  assert.deepEqual([...keyIds(rows, [{ value: (p) => p.window.xfp_pg, n: 2, dir: "desc" }])], ["b", "d"]);
});

test("axis domains are stable and include reference lines", () => {
  assert.deepEqual(symmetricDomain([-1, 2.5]), [-4, 4]);
  assert.deepEqual(symmetricDomain([-7.2, 3]), [-8, 8]);
  assert.deepEqual(zeroDomain([5, 9], 4, 14), [0, 16]);    // the 14 reference line plus headroom
});

test("names, weeks and pool sentences", () => {
  assert.equal(shortName("Justin Jefferson"), "J. Jefferson");
  assert.equal(shortName("Marvin Harrison Jr."), "M. Harrison");
  assert.equal(shortName("Amon-Ra St. Brown"), "A. St. Brown");
  assert.equal(weekList([1, 2, 3, 5, 7, 8]), "Weeks 1–3, 5, 7, 8");
  assert.equal(weekList([4]), "Week 4");
  assert.equal(hiddenSentence({ min_volume: 3, min_games: 1, team: 0, no_qualifying_games: 0 }), "3 below the volume filter and 1 with too few games are hidden.");
  assert.equal(hiddenSentence({ min_volume: 1 }), "1 below the volume filter is hidden.");
  assert.equal(hiddenSentence({}), "");
});

test("trade and waiver actions only where the league supports them", () => {
  assert.equal(tradeHref(null, 5), null);
  assert.equal(tradeHref({ availability_status: "ONTEAM", on_team_id: 3, my_team_id: 2, is_mine: false }, 5), "/trades?ta=2&tb=3&b=5");
  assert.equal(tradeHref({ availability_status: "ONTEAM", on_team_id: 2, my_team_id: 2, is_mine: true }, 5), "/trades?ta=2&a=5");
  assert.equal(tradeHref({ availability_status: "FREEAGENT", on_team_id: null, my_team_id: 2, is_mine: false }, 5), null);
  assert.equal(waiverHref({ availability_status: "WAIVERS", on_team_id: null, my_team_id: 2, is_mine: false }, "TE"), "/waivers?pos=TE&view=ALL");
  assert.equal(waiverHref({ availability_status: "ONTEAM", on_team_id: 1, my_team_id: 2, is_mine: false }, "TE"), null);
});
