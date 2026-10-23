import assert from "node:assert/strict";
import { test } from "node:test";
import {
  COMPARE_SORT_KEYS,
  compareApiPath,
  compareHref,
  contributionText,
  deltaMissingText,
  deltaSentence,
  parseCompareFilters,
  rangeHorizonNote,
  rangeText,
  sideMissingText,
  sideRankText,
  type CompareDelta,
  type CompareSide,
} from "./projection-compare.ts";

const query = (path: string) => new URLSearchParams(path.split("?")[1] ?? "");

const side = (over: Partial<CompareSide> & { rank?: Partial<CompareSide["rank"]> } = {}): CompareSide => ({
  value: 10, status: "ok", reason: null, ...over,
  rank: { overall: 12, overall_tied: false, position: 3, position_tied: false, position_label: "WR3", ...(over.rank ?? {}) },
});

const delta = (over: Partial<CompareDelta> = {}): CompareDelta => ({
  status: "ok", rank: 7, points: 4.2, points_per_week: 4.2, direction: "ours_higher", tied: false, relevant: true, large: true, ...over,
});

test("filters: defaults, valid values and the natural sort direction", () => {
  assert.deepEqual(parseCompareFilters({}), {
    pos: "ALL", horizon: "week", scope: "position", show: "all", status: "all", q: "",
    sort: { key: "disagreement", dir: "desc" }, offset: 0,
  });
  const f = parseCompareFilters({ pos: "WR", horizon: "ros", scope: "overall", show: "large", status: "available", q: "  Ja ", sort: "ours_rank", offset: "100" });
  assert.equal(f.pos, "WR");
  assert.equal(f.horizon, "ros");
  assert.equal(f.scope, "overall");
  assert.equal(f.show, "large");
  assert.equal(f.status, "available");
  assert.equal(f.q, "Ja");
  assert.deepEqual(f.sort, { key: "ours_rank", dir: "asc" });
  assert.equal(f.offset, 100);
});

test("filters: invalid values fall back instead of reaching the API", () => {
  const f = parseCompareFilters({ pos: "K", horizon: "year", scope: "team", show: "x", status: "mine", sort: "espn", dir: "up", offset: "-3" });
  assert.equal(f.pos, "ALL");
  assert.equal(f.horizon, "week");
  assert.equal(f.scope, "position");
  assert.equal(f.show, "all");
  assert.equal(f.status, "all");
  assert.deepEqual(f.sort, { key: "disagreement", dir: "desc" });
  assert.equal(f.offset, 0);
  assert.ok(COMPARE_SORT_KEYS.includes("provider_rank"));
});

test("api path sends every filter, so the cached answer follows the URL", () => {
  const f = parseCompareFilters({ pos: "TE", horizon: "ros", scope: "overall", show: "missing", status: "rostered", q: "ké", sort: "delta", dir: "asc", offset: "100" });
  const q = query(compareApiPath(f));
  assert.ok(compareApiPath(f).startsWith("/api/rankings/compare?"));
  assert.deepEqual(Object.fromEntries(q), {
    position: "TE", horizon: "ros", scope: "overall", show: "missing", status: "rostered", sort: "delta", dir: "asc",
    limit: "100", offset: "100", q: "ké",
  });
  assert.equal(query(compareApiPath(parseCompareFilters({}))).has("q"), false);
});

test("href keeps other params, drops the page on a filter change and keeps it when paging", () => {
  assert.equal(compareHref({ pos: "WR", offset: "100" }, { scope: "overall" }), "?pos=WR&scope=overall");
  assert.equal(compareHref({ pos: "WR", offset: "100" }, { offset: "200" }), "?pos=WR&offset=200");
  assert.equal(compareHref({ pos: "WR" }, { pos: null }), "?");
});

test("rank text: ties, scopes and a missing rank", () => {
  assert.equal(sideRankText(side(), "position"), "WR3");
  assert.equal(sideRankText(side({ rank: { position_tied: true } }), "position"), "T-WR3");
  assert.equal(sideRankText(side(), "overall"), "#12");
  assert.equal(sideRankText(side({ rank: { overall_tied: true } }), "overall"), "T-#12");
  assert.equal(sideRankText(side({ value: null, status: "missing", rank: { overall: null, position: null, position_label: null } }), "overall"), null);
});

test("a missing side is named in words, never as a number", () => {
  for (const status of ["bye", "missing", "incomplete", "no_estimate", "not_published", "unavailable", "other"]) {
    const t = sideMissingText(side({ status }));
    assert.ok(t.length > 0 && !/\d/.test(t), status);
  }
  assert.equal(sideMissingText(side({ status: "not_published" })), "Not published");
});

test("delta text: the missing side is named and a measured zero says the sources agree", () => {
  assert.equal(deltaMissingText(delta(), "ESPN"), null);
  assert.equal(deltaMissingText(delta({ status: "provider_missing", rank: null }), "ESPN"), "No ESPN rank");
  assert.equal(deltaMissingText(delta({ status: "provider_not_published", rank: null }), "Sleeper"), "Sleeper publishes none");
  assert.equal(deltaMissingText(delta({ status: "ours_unavailable", rank: null }), "ESPN"), "Ours unavailable");
  assert.equal(deltaSentence(delta(), "ESPN", "position"), "Ours ranks him 7 places higher than ESPN within his position");
  assert.equal(deltaSentence(delta({ rank: -1 }), "ESPN", "overall"), "ESPN ranks him 1 place higher than Ours overall");
  assert.equal(deltaSentence(delta({ rank: 0, tied: true }), "ESPN", "position"), "Same rank within his position from both sources (a tie)");
  assert.equal(deltaSentence(delta({ status: "ours_missing", rank: null }), "ESPN", "position"), null);
});

test("ranges: text only for an available range, and a rest-of-season page says the range is one week", () => {
  assert.equal(rangeText(null), null);
  assert.equal(rangeText({ available: false, reason: "QB held back" }), null);
  const r = {
    available: true as const, floor: 8.12, median: 18.4, ceiling: 33, centre: 20, week: 7, horizon: "Week 7", basis: "b",
    coverage: { nominal: 0.8, text: "t", confirmation: null, replay: null }, sample: { games_used: 6, latest_usage: null },
  };
  assert.equal(rangeText(r), "8.1–33.0");
  assert.equal(rangeHorizonNote("ros", 7), "Week 7 only; no rest-of-season range");
  assert.equal(rangeHorizonNote("week", 7), "Week 7");
});

test("contributions: signed points, a measured zero, and nothing when the run records none", () => {
  assert.equal(contributionText(-3.21), "−3.2");
  assert.equal(contributionText(1.05), "+1.1");
  assert.equal(contributionText(0.01), "0.0");
  assert.equal(contributionText(null), null);
});
