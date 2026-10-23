import assert from "node:assert/strict";
import test from "node:test";
import {
  COMPARE_UNAVAILABLE, MAX_SCENARIOS, addScenario, compareRouteError, conflictText, duplicateScenario, editingKey, findTrade, horizonMismatches, matchesRequest, orderedSides,
  parseTradeScenarios, readTradeScenarios, removeScenario, replaceScenario, saveState, rosterMoves, savedFreshness, scenarioAnalyzerHref, scenarioParam,
  scenariosBackendPath, scenariosSrc, serializeTradeScenarios, shortTermLabel, summaryText, tradeKey, tradeScenariosKey, writeTradeScenarios,
  type Scenario, type ScenarioCompareResponse, type ScenarioRow, type ScenarioSelection,
} from "./trade-scenarios.ts";

const A = "lg_aaaaaaaaaaaaaaaa";
const B = "lg_bbbbbbbbbbbbbbbb";
const AT = "2026-10-23T05:50:43Z";
const sel = (ta: number, tb: number, a: number[], b: number[], sw: number | null = null): ScenarioSelection => ({ ta, tb, a, b, sw });
const sc = (id: string, s: ScenarioSelection, dataVersion: string | null = "v1"): Scenario => ({ id, ...s, savedAt: AT, dataVersion });

function memory() {
  const map = new Map<string, string>();
  return { map, io: { getItem: (k: string) => map.get(k) ?? null, setItem: (k: string, v: string) => { map.set(k, v); } } };
}

test("the watchlist is keyed by canonical league and team; unscoped or teamless lists are never stored", () => {
  assert.equal(tradeScenariosKey(A, 2), "fi:trade-scenarios:v1:lg_aaaaaaaaaaaaaaaa:2");
  assert.notEqual(tradeScenariosKey(A, 2), tradeScenariosKey(B, 2));
  assert.notEqual(tradeScenariosKey(A, 2), tradeScenariosKey(A, 3));
  for (const [lg, team] of [[null, 2], ["12345", 2], ["lg_bad", 2], [A, null], [A, -1], [A, 1.5]] as const) {
    assert.equal(tradeScenariosKey(lg, team as number | null), null);
    assert.equal(editingKey(lg, team as number | null), null);
  }
  const { io, map } = memory();
  assert.equal(writeTradeScenarios(io, null, 2, [sc("s1", sel(2, 1, [207], [202]))]), false);
  assert.equal(map.size, 0);
});

test("three or more trades survive a reload without reselecting players", () => {
  const { io } = memory();
  const list = [sc("s1", sel(2, 1, [207], [202])), sc("s2", sel(2, 1, [207, 204], [303], 5)), sc("s3", sel(2, 3, [-16002], [208]), null)];
  assert.equal(writeTradeScenarios(io, A, 2, list), true);
  assert.deepEqual(readTradeScenarios(io, A, 2), { scenarios: list, available: true });
});

test("another league or team never sees the list, even a value copied under its key", () => {
  const { io, map } = memory();
  writeTradeScenarios(io, A, 2, [sc("s1", sel(2, 1, [207], [202]))]);
  assert.deepEqual(readTradeScenarios(io, B, 2).scenarios, []);
  assert.deepEqual(readTradeScenarios(io, A, 3).scenarios, []);
  map.set(tradeScenariosKey(B, 2)!, map.get(tradeScenariosKey(A, 2)!)!);
  assert.deepEqual(readTradeScenarios(io, B, 2).scenarios, []);
});

test("a malformed, oversized or tampered value is refused whole", () => {
  const good = JSON.parse(serializeTradeScenarios(A, 2, [sc("s1", sel(2, 1, [207], [202]))]));
  const bad = (patch: Record<string, unknown>) => JSON.stringify({ ...good, scenarios: [{ ...good.scenarios[0], ...patch }] });
  for (const patch of [{ ta: 1 }, { a: [] }, { a: [202] }, { a: [0] }, { a: ["207"] }, { sw: 0 }, { id: "<x>" }, { savedAt: "yesterday" },
    { dataVersion: 5 }, { dataVersion: "has space" }, { a: Array.from({ length: 13 }, (_, i) => i + 1) }]) {
    assert.deepEqual(parseTradeScenarios(bad(patch), A, 2), [], JSON.stringify(patch));
  }
  const six = JSON.stringify({ ...good, scenarios: Array.from({ length: 6 }, (_, i) => ({ ...good.scenarios[0], id: `s${i}`, a: [300 + i] })) });
  assert.deepEqual(parseTradeScenarios(six, A, 2), []);
  assert.deepEqual(parseTradeScenarios("{not json", A, 2), []);
  assert.deepEqual(parseTradeScenarios(JSON.stringify({ ...good, v: 2 }), A, 2), []);
  // Unknown fields (a stored result, names) are dropped on write: only the sanitized record is kept.
  const extra = serializeTradeScenarios(A, 2, [{ ...sc("s1", sel(2, 1, [207], [202])), ros: 12.5 } as Scenario]);
  assert.equal(extra.includes("ros"), false);
});

test("storage that throws leaves the page usable", () => {
  const throwing = { getItem: () => { throw new Error("denied"); }, setItem: () => { throw new Error("full"); } };
  assert.deepEqual(readTradeScenarios(throwing, A, 2), { scenarios: [], available: false });
  assert.equal(writeTradeScenarios(throwing, A, 2, []), false);
});

test("save, duplicate, update and remove, up to five scenarios", () => {
  const meta = (id: string) => ({ id, savedAt: AT, dataVersion: "v2" });
  let r = addScenario([], sel(2, 1, [207], [202]), meta("s1"));
  assert.equal(r.outcome, "added");
  // The same trade, even with the sides named the other way round, is not saved twice.
  assert.deepEqual(addScenario(r.scenarios, sel(1, 2, [202], [207]), meta("s9")).outcome, "duplicate");
  r = duplicateScenario(r.scenarios, "s1", { id: "s2", savedAt: AT });
  assert.deepEqual(r.scenarios.map((s) => s.id), ["s1", "s2"]);
  assert.equal(r.scenarios[1].dataVersion, "v2");
  const up = replaceScenario(r.scenarios, "s2", sel(2, 1, [207], [303], 6), { savedAt: AT, dataVersion: "v3" });
  assert.equal(up.outcome, "updated");
  assert.deepEqual(up.scenarios[1], { id: "s2", ta: 2, tb: 1, a: [207], b: [303], sw: 6, savedAt: AT, dataVersion: "v3" });
  assert.equal(replaceScenario(up.scenarios, "s2", sel(2, 1, [207], [202]), { savedAt: AT, dataVersion: "v3" }).outcome, "duplicate");
  let list = up.scenarios;
  for (let i = 3; i <= MAX_SCENARIOS; i++) list = addScenario(list, sel(2, 1, [300 + i], [202]), meta(`s${i}`)).scenarios;
  assert.equal(list.length, MAX_SCENARIOS);
  assert.equal(addScenario(list, sel(2, 1, [399], [202]), meta("s9")).outcome, "full");
  assert.equal(duplicateScenario(list, "s1", { id: "s9", savedAt: AT }).outcome, "full");
  assert.deepEqual(removeScenario(list, "s2").map((s) => s.id), ["s1", "s3", "s4", "s5"]);
  assert.equal(addScenario([], sel(2, 2, [207], [202]), meta("s1")).outcome, "invalid");
});

test("a trade's identity ignores player order and which side is A", () => {
  assert.equal(tradeKey(sel(2, 1, [207, 204], [202])), tradeKey(sel(1, 2, [202], [204, 207])));
  assert.notEqual(tradeKey(sel(2, 1, [207], [202])), tradeKey(sel(2, 1, [207], [303])));
  assert.equal(findTrade([sc("s1", sel(2, 1, [207], [202]))], sel(1, 2, [202], [207]))?.id, "s1");
});

test("players no longer on the sending roster are caught from the page's rosters", () => {
  const teams = [
    { team_id: 1, name: "One", roster: [{ espn_player_id: 202, name: "Foxtrot" }] },
    { team_id: 2, name: "Two", roster: [{ espn_player_id: 204, name: "Juliet" }] },
    { team_id: 3, name: "Three", roster: [{ espn_player_id: 207, name: "Kilo" }] },
  ];
  assert.deepEqual(rosterMoves(sel(2, 1, [207, 204], [202, 999]), teams), [{ id: 207, from: 2, now: 3 }, { id: 999, from: 1, now: null }]);
  assert.deepEqual(rosterMoves(sel(2, 1, [204], [202]), teams), []);
});

test("a scenario is current only when saved on the version the page reads", () => {
  assert.equal(savedFreshness("v1", "v1"), "current");
  assert.equal(savedFreshness("v1", "v2"), "different");
  assert.equal(savedFreshness(null, "v2"), "unknown");
  assert.equal(savedFreshness("v1", null), "unknown");
});

test("the comparison request is league-scoped, ordered and validated before it reaches the backend", () => {
  const list = [sel(2, 1, [207, 204], [202]), sel(2, 3, [-16002], [208])];
  const src = scenariosSrc(A, list, 5);
  assert.equal(src, `/l/${A}/api/trade-scenarios?scenarios=2%3A1%3A207.204%3A202%2C2%3A3%3A-16002%3A208&start_week=5`);
  assert.notEqual(scenariosSrc(A, [list[1], list[0]], 5), src);           // order is part of the request
  assert.notEqual(scenariosSrc(A, list, null), src);                      // so is the horizon
  const params = new URL(src, "http://x").searchParams;
  assert.equal(scenariosBackendPath(params), "/api/trades/scenarios?scenarios=2%3A1%3A207.204%3A202%2C2%3A3%3A-16002%3A208&start_week=5");
  for (const q of ["", "scenarios=", "scenarios=2:1:207", "scenarios=2:1:207:202&start_week=0", "scenarios=2:1:207:202&start_week=x",
    `scenarios=${Array(6).fill("2:1:207:202").join(",")}`, "scenarios=2:1:207:202,2:1:a:b"]) {
    assert.equal(scenariosBackendPath(new URLSearchParams(q)), null, q);
  }
  // Only the scenario list and the start week are forwarded.
  assert.equal(scenariosBackendPath(new URLSearchParams("scenarios=2:1:207:202&league=lg_bbbbbbbbbbbbbbbb")), "/api/trades/scenarios?scenarios=2%3A1%3A207%3A202");
  assert.equal(scenarioParam(list[0]), "2:1:207.204:202");
});

test("the analyzer link reproduces the scenario", () => {
  assert.equal(scenarioAnalyzerHref(sel(2, 1, [207, 204], [202])), "/trades?ta=2&tb=1&a=207&a=204&b=202");
  assert.equal(scenarioAnalyzerHref(sel(2, 1, [207], [202], 6)), "/trades?ta=2&tb=1&a=207&b=202&sw=6");
});

test("rows saved with another start week are named, since every row shares one horizon", () => {
  const list = [sc("s1", sel(2, 1, [207], [202])), sc("s2", sel(2, 1, [207], [303], 6))];
  assert.deepEqual(horizonMismatches(list, null), [1]);
  assert.deepEqual(horizonMismatches(list, 6), [0]);
});

const row = (ta: number, tb: number, a: number[], b: number[]): ScenarioRow => ({
  index: 0, request: { team_a_id: ta, team_b_id: tb, team_a_sends: a, team_b_sends: b }, shape: "1-for-1", status: "evaluated",
  reason: null, duplicate_of: null, conflicts_with: [], moved: [],
  team_a: { team_id: ta } as ScenarioRow["team_a"], team_b: { team_id: tb } as ScenarioRow["team_b"],
});

test("only an answer for this league and exactly this list is shown", () => {
  const list = [sel(2, 1, [207], [202])];
  const res = { contract: "trade-scenario-compare/v1", scope: A, scenarios: [row(2, 1, [207], [202])] } as ScenarioCompareResponse;
  assert.equal(matchesRequest(res, A, list), true);
  assert.equal(matchesRequest({ ...res, scope: B }, A, list), false);
  assert.equal(matchesRequest(res, A, [sel(2, 1, [207], [303])]), false);
  assert.equal(matchesRequest(res, A, [...list, sel(2, 1, [207], [303])]), false);
  assert.equal(matchesRequest(null, A, list), false);
});

test("the page's team is shown first when it is in the trade", () => {
  const r = row(2, 1, [207], [202]);
  assert.deepEqual(orderedSides(r, 1).map((s) => s.team_id), [1, 2]);
  assert.deepEqual(orderedSides(r, 2).map((s) => s.team_id), [2, 1]);
  assert.deepEqual(orderedSides(r, 7).map((s) => s.team_id), [2, 1]);
  assert.deepEqual(orderedSides({ ...r, status: "stale", team_a: undefined, team_b: undefined }, 1), []);
});

test("a shared player is an alternative, never a sum", () => {
  assert.equal(conflictText({ scenarios: [0, 2] }, "Kilo RB"), "Scenarios 1 and 3 both include Kilo RB: at most one of them can happen as saved.");
  assert.equal(conflictText({ scenarios: [0, 1, 2] }, "Kilo RB"), "Scenarios 1, 2 and 3 all include Kilo RB: at most one of them can happen as saved.");
});

test("a start week change on a saved trade is an update, never hidden as already saved", () => {
  const list = [sc("s1", sel(2, 1, [207], [202])), sc("s2", sel(2, 1, [207], [303], 6))];
  assert.deepEqual(saveState(list, sel(2, 1, [207], [202]), null), { kind: "saved", index: 0 });
  assert.deepEqual(saveState(list, sel(1, 2, [202], [207]), null), { kind: "saved", index: 0 });
  assert.deepEqual(saveState(list, sel(2, 1, [207], [202], 7), "s2"), { kind: "start_week", index: 0, id: "s1", savedWeek: null });
  assert.deepEqual(saveState(list, sel(2, 1, [207], [303]), null), { kind: "start_week", index: 1, id: "s2", savedWeek: 6 });
  // Update keeps the scenario's place and ID and stores the new start week; the format is unchanged.
  const up = replaceScenario(list, "s2", sel(2, 1, [207], [303]), { savedAt: AT, dataVersion: "v2" });
  assert.equal(up.outcome, "updated");
  assert.deepEqual(up.scenarios[1], { id: "s2", ta: 2, tb: 1, a: [207], b: [303], sw: null, savedAt: AT, dataVersion: "v2" });
  assert.deepEqual(parseTradeScenarios(serializeTradeScenarios(A, 2, up.scenarios), A, 2), up.scenarios);
  assert.deepEqual(saveState(list, sel(2, 1, [207], [208]), "s2"), { kind: "new", editing: { index: 1, id: "s2" } });
  assert.deepEqual(saveState(list, sel(2, 1, [207], [208]), "gone"), { kind: "new", editing: null });
});

test("a failed comparison never sends the internal API address or start hint to the browser", () => {
  // What api() answers when the backend is down: its message names the internal URL and how to start the server.
  const offline = { ok: false, data: null, kind: "offline",
    error: "Cannot reach the backend at http://127.0.0.1:8000. Start it with: cd backend && uv run uvicorn app.main:app --reload" };
  const out = compareRouteError(offline);
  assert.deepEqual(out, { body: { error: COMPARE_UNAVAILABLE, kind: "offline" }, status: 502 });
  assert.equal(/127\.0\.0\.1|uvicorn/.test(JSON.stringify(out.body)), false);
  assert.equal(compareRouteError({ error: "Unexpected token < in JSON at http://api.internal:8000", kind: "unknown", status: 200 }).body.error, COMPARE_UNAVAILABLE);
  assert.equal(compareRouteError({ error: "Internal Server Error", kind: "http", status: 500 }).body.error, COMPARE_UNAVAILABLE);
  assert.equal(compareRouteError({ error: "schema too old", kind: "schema", status: 503 }).body.error, COMPARE_UNAVAILABLE);
  for (const status of [404, 409, 422]) {
    assert.deepEqual(compareRouteError({ error: "start_week must be >= 3", kind: "http", status }), { body: { error: "start_week must be >= 3", kind: "http" }, status });
  }
});

test("the first week is called next week only when the comparison starts at the earliest open week", () => {
  assert.equal(shortTermLabel({ short_term_week: 3, start_week_overridden: false }), "Next week");
  assert.equal(shortTermLabel({ short_term_week: 5, start_week_overridden: true }), "Week 5");
  assert.equal(shortTermLabel({ short_term_week: null, start_week_overridden: false }), "First week");
});

test("the summary tells a trade that is not valid from one that could not be evaluated", () => {
  assert.equal(summaryText({ scenarios: 4, evaluated: 1, incomplete: 0, stale: 1, invalid: 1, unavailable: 1 }),
    "1 of 4 evaluated · 1 out of date · 1 not valid · 1 not evaluated");
  assert.equal(summaryText({ scenarios: 2, evaluated: 2, incomplete: 1, stale: 0, invalid: 0, unavailable: 0 }), "2 of 2 evaluated · 1 incomplete");
});
