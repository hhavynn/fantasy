import assert from "node:assert/strict";
import test from "node:test";
import {
  MAX_FILTERS, apiQuery, carried, cellTitle, columns, countsSentence, filterText, formRows, formatValue, isPreset,
  parseState, presetHref, sampleNote, waiverHref, weeksText, type MetricInfo, type Ownership,
} from "./screener.ts";

// Invented metrics shaped like the screener/v1 registry.
const metric = (key: string, unit: MetricInfo["unit"] = "per_game", short = key): MetricInfo => ({
  key, label: key, short, positions: ["WR", "RB", "TE"], unit, unit_label: unit === "percent" ? "%" : "per game",
  aggregation: "mean", inputs: [key], denominator: "games", source: "test", bounds: [0, 100], definition: "",
});
const KNOWN = [metric("xfp_pg"), metric("ppr_pg"), metric("fpoe_pg"), metric("targets_pg"), metric("target_share", "percent", "Tgt %"),
  metric("route_participation", "percent", "Route %"), metric("snap_share", "percent"), metric("routes_pg"), metric("carries_pg")];

test("filter rows come from aligned fg/fm/fo/fv params and compile to group:metric:op:value tokens", () => {
  const s = parseState({ pos: ["WR", "QB"], fg: ["1", "1", "2"], fm: ["route_participation", "", "xfp_pg"], fo: ["gte", "lte", "gt"],
    fv: ["70", "3", "12.5"], last: "3", min_games: "3" });
  assert.deepEqual(s.errors, []);
  assert.deepEqual(s.pos, ["WR"]);                     // unknown positions dropped
  assert.equal(s.rows.length, 2);                      // a row without a metric is ignored
  assert.equal(apiQuery(s), "pos=WR&last=3&min_games=3&f=1%3Aroute_participation%3Agte%3A70&f=2%3Axfp_pg%3Agt%3A12.5");
});

test("malformed values stop the screen instead of being guessed", () => {
  for (const fv of ["", "abc", "1e9", "12.345", "70%", "1;DROP"]) {
    const s = parseState({ fm: "xfp_pg", fv });
    assert.equal(s.errors.length, 1, fv);
  }
  assert.equal(parseState({ fm: "Xfp; drop", fv: "1" }).errors.length, 1);
  assert.equal(parseState({ fm: "xfp_pg", fv: "1", fg: "9" }).errors.length, 1);
  assert.equal(parseState({ fm: "xfp_pg", fv: "1", fo: "eq" }).errors.length, 1);
  assert.equal(parseState({ from: "5", to: "2" }).errors.length, 1);
  assert.equal(parseState({ from: "0" }).errors.length, 1);
  assert.equal(parseState({ to: "19" }).errors.length, 1);
  const many = parseState({ fm: Array(MAX_FILTERS + 1).fill("xfp_pg"), fv: Array(MAX_FILTERS + 1).fill("1") });
  assert.ok(many.errors.some((e) => e.includes(`At most ${MAX_FILTERS}`)));
});

test("scalars outside their ranges fall back to the backend defaults", () => {
  const s = parseState({ owner: "everyone", sort: "x; DROP", dir: "up", limit: "500" });
  assert.equal(s.owner, "any");
  assert.equal(s.sort, null);
  assert.equal(s.dir, null);
  assert.equal(s.limit, null);
  assert.equal(apiQuery(s), "");
});

test("a preset's backend query round-trips through the page URL", () => {
  const query = { pos: ["WR"], last: 3, min_games: 3, f: ["1:route_participation:gte:70", "1:targets_pg:gte:5"], sort: "targets_pg", dir: "desc" };
  const href = presetHref(query);
  assert.ok(href.startsWith("/screener?"));
  const sp = Object.fromEntries([...new URLSearchParams(href.split("?")[1]).keys()].map((k) => [k, new URLSearchParams(href.split("?")[1]).getAll(k)]));
  const s = parseState(sp);
  assert.deepEqual(s.errors, []);
  assert.ok(isPreset(s, query));
  assert.ok(!isPreset({ ...s, owner: "available" }, query));
  assert.ok(apiQuery(s).includes("f=1%3Aroute_participation%3Agte%3A70"));
  assert.equal(presetHref({}), "/screener");
});

test("form rows add two blanks but never pass the cap", () => {
  assert.equal(formRows(parseState({})).length, 2);
  const full = parseState({ fm: Array(MAX_FILTERS).fill("xfp_pg"), fv: Array(MAX_FILTERS).fill("1") });
  assert.equal(formRows(full).length, MAX_FILTERS);
});

test("values keep their unit and missing stays missing", () => {
  assert.equal(formatValue(71.25, "percent"), "71.25%");
  assert.equal(formatValue(21.96, "percent"), "21.96%");   // never "22.0%": the filter compares two decimals
  assert.equal(formatValue(5.333, "per_game"), "5.33");
  assert.equal(formatValue(-2.5, "points_per_game"), "-2.50");
  assert.equal(formatValue(0, "per_game"), "0.00");       // a measured zero is a number
  assert.equal(formatValue(null, "per_game"), null);
  assert.equal(filterText({ group: 1, metric: "target_share", op: "gte", value: "22" }, KNOWN[4]), "Tgt % ≥ 22%");
});

test("sample notes and titles say how many games a value used and why one is missing", () => {
  assert.equal(sampleNote({ value: 3, n: 4, of: 4 }), null);
  assert.equal(sampleNote({ value: 3, n: 2, of: 3 }), "2 of 3 games");
  assert.match(cellTitle({ value: null, n: 0, of: 3, reason: "no route data" }, KNOWN[5]), /missing: no route data\. Never counted as 0/);
  assert.match(cellTitle({ value: 80, n: 3, of: 3, percentile: 87, pool: 40 }, KNOWN[5]), /87th percentile among 40 players/);
  assert.match(cellTitle(undefined, KNOWN[8]), /not defined for this position/);
});

test("result columns: filtered metrics first, then the sort, then defaults, without duplicates", () => {
  const s = parseState({ fm: ["target_share", "xfp_pg"], fv: ["20", "10"] });
  assert.deepEqual(columns(s, "snap_share", KNOWN).map((m) => m.key), ["target_share", "xfp_pg", "snap_share", "ppr_pg", "fpoe_pg", "targets_pg"]);
  assert.deepEqual(columns(parseState({}), "unknown", KNOWN).map((m) => m.key), ["xfp_pg", "ppr_pg", "fpoe_pg", "targets_pg"]);
});

test("only a known available player links to the add/drop simulation", () => {
  const own = (o: Partial<Ownership>): Ownership => ({ status: "unknown", label: "Roster status unknown", available: false, player_id: null,
    mapping: null, team_id: null, team_name: null, is_mine: false, ...o });
  assert.equal(waiverHref(own({ status: "free_agent", available: true, player_id: 12 })), "/waivers?add=12#add-drop");
  assert.equal(waiverHref(own({ status: "unknown", player_id: 12 })), null);
  assert.equal(waiverHref(own({ status: "rostered", player_id: 12 })), null);
});

test("weeks, counts and carried params read plainly", () => {
  assert.equal(weeksText([2, 3, 4]), "Weeks 2–4");
  assert.equal(weeksText([1, 3]), "Weeks 1, 3");
  assert.equal(weeksText([5]), "Week 5");
  assert.equal(countsSentence({ pool: 40, below_min_games: 3, below_min_routes: 0, routes_unknown: 0, matched: 7, not_matched: 25,
    incomplete: 5, ownership_unknown: 2, ownership_excluded: 0, shown: 7 }),
  "7 of 40 players matched; 3 below the minimum games; 5 could not be decided (missing data).");
  assert.equal(countsSentence({ pool: 40, below_min_games: 0, below_min_routes: 0, routes_unknown: 0, matched: 7, not_matched: 33,
    incomplete: 0, ownership_unknown: 1, ownership_excluded: 3, shown: 4 }),
  "4 of 40 players matched; 3 more matched but left out by the ownership filter.");   // the page's "Matched" figure
  assert.deepEqual(carried({ sort: "xfp_pg", dir: "asc", limit: "25", fm: "x" }), [["sort", "xfp_pg"], ["dir", "asc"]]);
});
