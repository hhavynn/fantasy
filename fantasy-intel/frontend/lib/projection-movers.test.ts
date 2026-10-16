import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  CONTRACT, DEFAULT_SORT, PCT_MISSING, SORT_KEYS, beforeAfterText, biggestMovers, chartMovers, eyebrowText,
  freshnessText, groupSignals, informationWindowText, moversHref, moversQuery, notComparableLines, parseMoversState,
  pctText, rankText, rankTitle, reasonCounts, selectedMover, signalSample, signalValue, signedPts, snapshotName,
  moverSortValue, totalsText, weekNote, weekSeries, weekSpan, windowText,
  type EvidenceSignal, type Mover, type MoversResponse,
} from "./projection-movers.ts";
import { sortRows, type SortDir } from "./sort.ts";

// The synthetic fixture follows the projection-movers/v1 contract with invented players ("Test Player A"...).
const espn = JSON.parse(readFileSync(new URL("./fixtures/projection-movers.synthetic.json", import.meta.url), "utf8")) as MoversResponse;

const unavailable: MoversResponse = {
  ...espn,
  available: false,
  reason: "Only one stored forecast exists, so there is nothing to compare yet.",
  baselines: [], baseline: null, current: null,
  window: { weeks: [], excluded_weeks: [], scoring_verified: false, scoring_note: null },
  summary: { compared: 0, risers: 0, fallers: 0, unchanged: 0, not_comparable: 0, not_comparable_reasons: {} },
  risers: [], fallers: [], players: [],
};

function oursMover(over: Partial<Mover> = {}): Mover {
  return {
    ...espn.risers[0],
    name: "Test Player X",
    espn_player_id: 910001,
    weeks: [
      { week: 8, before: 9.0, after: 10.0, delta: 1.0, bye: false, status_before: "projected", status_after: "projected", horizon_before: 1, horizon_after: 0 },
      { week: 9, before: 0, after: 0, delta: 0, bye: true, status_before: "bye", status_after: "bye", horizon_before: 2, horizon_after: 1 },
      { week: 10, before: 0, after: 8.0, delta: 8.0, bye: false, status_before: "withheld_unavailable", status_after: "projected", horizon_before: 3, horizon_after: 2 },
    ],
    components: { availability: 1.2, per_game: 7.8 },
    ...over,
  };
}

const ours: MoversResponse = {
  ...espn,
  source: "independent", view: "week", label: "Fantasy Intel projections (test)", experimental: true,
  governance: {
    production_version: "test-model", fallback_version: "test-fallback", served_version: "test-model", is_fallback: false,
    production_approval: "owner_override", espn_superiority_established: false, start_sit_ready: false,
    production_evidence: "Historical research only.", status_note: null,
  },
  baselines: [{ id: "w5", label: "Forecast from week 5", taken_at: "2026-10-07T12:00:00+00:00", cycle_week: 5 }],
  baseline: { id: "w5", label: "Forecast from week 5", taken_at: "2026-10-07T12:00:00+00:00", cycle_week: 5, model_version: "test-model", run_ids: { "8": "run-a" } },
  current: { id: "w6", label: "Forecast from week 6", taken_at: "2026-10-14T12:00:00+00:00", cycle_week: 6, model_version: "test-model", run_ids: { "8": "run-b" } },
  window: { weeks: [8], excluded_weeks: [], scoring_verified: true, scoring_note: null },
  skipped: [{ run_id: "run-x", week: 8, reason: "made after the first kickoff" }, { run_id: "run-y", week: 9, reason: "made after the first kickoff" }],
  excluded_runs: [{ run_id: "run-z", week: 8, reason: "made under different league scoring" }],
  risers: [oursMover()], fallers: [], players: [oursMover({ evidence: null })],
};

// ----------------------------------------------------------------------------- fixture and contract

test("the fixture follows the contract and uses only invented players", () => {
  assert.equal(espn.contract, CONTRACT);
  assert.equal(espn.source, "espn");
  assert.equal(espn.governance, null);
  for (const m of [...espn.risers, ...espn.fallers, ...espn.players]) assert.match(m.name, /^Test Player [A-Z]$/);
});

test("the fixture's numbers agree with each other the way the contract says", () => {
  const n = espn.window.weeks.length;
  for (const m of [...espn.risers, ...espn.fallers, ...espn.players]) {
    assert.ok(Math.abs(m.after - m.before - m.delta) < 0.011, `${m.name} delta`);
    assert.ok(Math.abs(m.per_week_before - m.before / n) < 0.011, `${m.name} per week`);
    assert.equal(m.rank_change, (m.rank_before ?? 0) - (m.rank_after ?? 0), `${m.name} rank change`);
    assert.deepEqual(m.weeks.map((w) => w.week), espn.window.weeks);
    const dir = m.delta >= espn.thresholds.flat ? "up" : m.delta <= -espn.thresholds.flat ? "down" : "flat";
    assert.equal(m.direction, dir, `${m.name} direction`);
    if (m.pct !== null) assert.ok(m.before >= espn.thresholds.pct_floor);
  }
  assert.ok(espn.risers.every((m) => m.relevant && m.delta >= espn.thresholds.min_delta));
  assert.ok(espn.fallers.every((m) => m.relevant && m.delta <= -espn.thresholds.min_delta));
  assert.ok(espn.players.every((m) => m.evidence === null), "the full table carries no evidence");
});

// ----------------------------------------------------------------------------- URL state

test("parseMoversState defaults to ESPN, rest of season, all positions", () => {
  assert.deepEqual(parseMoversState({}), { source: "espn", view: "ros", position: null, baseline: null, player: null });
  assert.deepEqual(parseMoversState(undefined), { source: "espn", view: "ros", position: null, baseline: null, player: null });
});

test("parseMoversState accepts the documented values, with ours as an alias", () => {
  assert.deepEqual(
    parseMoversState({ source: "independent", view: "week", position: "wr", baseline: "2026-10-14", player: "4262921" }),
    { source: "independent", view: "week", position: "WR", baseline: "2026-10-14", player: 4262921 },
  );
  assert.equal(parseMoversState({ source: "ours" }).source, "independent");
  assert.equal(parseMoversState({ baseline: "w6" }).baseline, "w6");
});

test("parseMoversState drops anything the API would reject", () => {
  const s = parseMoversState({ source: "nope", view: "year", position: "K", baseline: "yesterday", player: "-3" });
  assert.deepEqual(s, { source: "espn", view: "ros", position: null, baseline: null, player: null });
  assert.equal(parseMoversState({ baseline: "2026-10-14; drop" }).baseline, null);
  assert.equal(parseMoversState({ player: "12abc" }).player, null);
  assert.equal(parseMoversState({ player: "0" }).player, null);
});

test("parseMoversState takes the first of a repeated parameter", () => {
  assert.equal(parseMoversState({ position: ["RB", "WR"], view: ["week", "ros"] }).position, "RB");
  assert.equal(parseMoversState({ view: ["week", "ros"] }).view, "week");
});

test("moversQuery always sends source and view and sends the rest only when chosen", () => {
  const base = parseMoversState({});
  assert.equal(moversQuery(base), "source=espn&view=ros&limit=15");
  assert.equal(
    moversQuery({ source: "independent", view: "week", position: "TE", baseline: "w5", player: 7 }, 10),
    "source=independent&view=week&limit=10&position=TE&baseline=w5",
  );
});

test("moversHref keeps the plain page plain and clears the player when a filter changes", () => {
  const state = parseMoversState({ position: "WR", player: "900001" });
  assert.equal(moversHref(parseMoversState({})), "/movers");
  assert.equal(moversHref(state, { position: null }), "/movers");
  assert.equal(moversHref(state, { view: "week" }), "/movers?view=week&position=WR");
  assert.equal(moversHref(state, { player: 900002 }), "/movers?position=WR&player=900002");
  assert.equal(moversHref(state, { source: "independent" }), "/movers?source=independent&position=WR");
  assert.equal(moversHref(state, { baseline: "2026-10-13" }), "/movers?position=WR&baseline=2026-10-13");
});

// ----------------------------------------------------------------------------- wording

test("snapshotName turns ids into dates and weeks without time zones", () => {
  assert.equal(snapshotName({ id: "2026-10-15", label: "x", taken_at: null, cycle_week: null }), "Oct 15");
  assert.equal(snapshotName({ id: "2026-01-02", label: "x", taken_at: null, cycle_week: null }), "Jan 2");
  assert.equal(snapshotName({ id: "w6", label: "x", taken_at: null, cycle_week: null }), "week 6");
  assert.equal(snapshotName({ id: "odd", label: "Odd one", taken_at: null, cycle_week: null }), "Odd one");
});

test("weekSpan collapses runs of consecutive weeks", () => {
  assert.equal(weekSpan([7, 8, 9, 11]), "7–9, 11");
  assert.equal(weekSpan([9, 7, 8]), "7–9");
  assert.equal(weekSpan([]), "");
});

test("windowText names the weeks, collapsing runs", () => {
  assert.equal(windowText([7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17]), "weeks 7–17");
  assert.equal(windowText([8]), "week 8 only");
  assert.equal(windowText([7, 9, 10]), "weeks 7, 9–10");
  assert.equal(windowText([]), null);
});

test("freshnessText says which stored forecasts are compared and over which weeks", () => {
  assert.equal(freshnessText(espn), "ESPN snapshot Oct 15 vs Oct 14 · weeks 7–9");
  assert.equal(freshnessText(ours), "Fantasy Intel forecast from week 6 vs week 5 · week 8 only");
  assert.equal(freshnessText(unavailable), null);
});

test("eyebrowText shows the week when known", () => {
  assert.equal(eyebrowText(espn), "Projections · week 6");
  assert.equal(eyebrowText(unavailable), "Projections");
  assert.equal(eyebrowText(null), "Projections");
});

test("rankText shows the position rank move and names a missing rank", () => {
  const a = espn.risers.find((m) => m.name === "Test Player A")!;
  assert.equal(rankText(a), "WR14 → WR9");
  assert.equal(rankText({ position: "WR", rank_before: 40, rank_after: 40 }), "WR40, no change");
  assert.equal(rankText({ position: "RB", rank_before: null, rank_after: 3 }), null);
  assert.match(rankTitle(a), /Moved up 5 places among WRs/);
  assert.match(rankTitle({ position: "RB", rank_before: 3, rank_after: 4, rank_change: -1 }), /Moved down 1 place among RBs/);
  assert.match(rankTitle({ position: "RB", rank_before: null, rank_after: 4, rank_change: null }), /not ranked in both/);
});

test("signedPts and pctText use a real minus sign and keep zero unsigned", () => {
  assert.equal(signedPts(11.5), "+11.5");
  assert.equal(signedPts(-4.25), "−4.3");
  assert.equal(signedPts(0.04), "0.0");
  assert.equal(signedPts(null), null);
  assert.equal(pctText(0.0955), "+9.6%");
  assert.equal(pctText(-0.2), "−20.0%");
  assert.equal(pctText(0), "0.0%");
  assert.equal(pctText(null), null);
  assert.match(PCT_MISSING, /too small/);
});

test("beforeAfterText and totalsText", () => {
  assert.equal(beforeAfterText({ before: 120.44, after: 131.9 }), "120.4 → 131.9");
  assert.equal(totalsText([7, 8, 9, 10]), "Projected points summed over weeks 7–10");
  assert.equal(totalsText([8]), "Projected points for week 8");
  assert.equal(totalsText([]), "Projected points");
});

test("reasonCounts merges identical sentences", () => {
  assert.deepEqual(reasonCounts(ours.skipped), [{ reason: "made after the first kickoff", n: 2 }]);
  assert.deepEqual(reasonCounts([{ reason: "a" }, { reason: "b" }, { reason: "a" }]), [{ reason: "a", n: 2 }, { reason: "b", n: 1 }]);
  assert.deepEqual(reasonCounts([]), []);
});

test("notComparableLines orders by size and caps the list", () => {
  const r = notComparableLines(espn.summary);
  assert.deepEqual(r.lines.map((l) => l.n), [2, 1]);
  assert.equal(r.more, 0);
  const many = { ...espn.summary, not_comparable_reasons: { a: 1, b: 5, c: 3, d: 2, e: 4, f: 0 } };
  const capped = notComparableLines(many, 3);
  assert.deepEqual(capped.lines.map((l) => l.reason), ["b", "e", "c"]);
  assert.equal(capped.more, 2);
});

// ----------------------------------------------------------------------------- selection and series

test("biggestMovers reads the head of each ranked list", () => {
  const b = biggestMovers(espn);
  assert.equal(b.riser?.name, "Test Player B");
  assert.equal(b.faller?.name, "Test Player D");
  assert.deepEqual(biggestMovers(unavailable), { riser: null, faller: null });
});

test("selectedMover defaults to the top riser, then the top faller, then the largest change", () => {
  assert.equal(selectedMover(espn, null).mover?.name, "Test Player B");
  assert.equal(selectedMover({ ...espn, risers: [] }, null).mover?.name, "Test Player D");
  assert.equal(selectedMover({ ...espn, risers: [], fallers: [] }, null).mover?.name, espn.players[0].name);
  assert.equal(selectedMover(unavailable, null).mover, null);
});

test("selectedMover honours a requested player and prefers the copy that carries evidence", () => {
  const a = selectedMover(espn, 900001);
  assert.equal(a.mover?.name, "Test Player A");
  assert.equal(a.requestedMissing, false);
  assert.ok(a.mover?.evidence, "found among the risers, so evidence is attached");
  const g = selectedMover(espn, 900007);
  assert.equal(g.mover?.name, "Test Player G");
  assert.equal(g.mover?.evidence, null, "table-only players have no evidence");
});

test("selectedMover falls back to the default and says the request was not found", () => {
  const r = selectedMover(espn, 123456789);
  assert.equal(r.requestedMissing, true);
  assert.equal(r.mover?.name, "Test Player B");
});

test("chartMovers lists risers then fallers, largest change first, without duplicates", () => {
  const rows = chartMovers(espn);
  assert.deepEqual(rows.map((r) => r.value), [7.5, 6.5, 3.6, -4.8, -6.8, -8.8]);
  assert.equal(rows[0].label, "Test Player B");
  const dup = chartMovers({ ...espn, fallers: [espn.risers[0]] });
  assert.equal(dup.filter((r) => r.id === espn.risers[0].espn_player_id).length, 1);
  assert.equal(chartMovers(espn, 1).length, 2);
  assert.deepEqual(chartMovers(unavailable), []);
});

test("weekSeries leaves a bye week as a gap, not a projected zero", () => {
  const s = weekSeries(ours.risers[0]);
  assert.deepEqual(s.labels, ["8", "9", "10"]);
  assert.deepEqual(s.before, [9.0, null, 0]);
  assert.deepEqual(s.after, [10.0, null, 8.0]);
  const plain = weekSeries(espn.risers[0]);
  assert.equal(plain.before.length, espn.window.weeks.length);
  assert.ok(plain.before.every((v) => v !== null));
});

test("weekNote explains byes, status changes and horizons, and is silent for a plain week", () => {
  const [w8, w9, w10] = ours.risers[0].weeks;
  assert.equal(weekNote(w9), "Bye week");
  assert.equal(weekNote(w8), "Made 1 week ahead → next-week forecast");
  assert.equal(weekNote(w10), "Status: withheld unavailable → projected · Made 3 weeks ahead → 2 weeks ahead");
  assert.equal(weekNote(espn.risers[0].weeks[0]), null);
});

// ----------------------------------------------------------------------------- evidence

const sig = (over: Partial<EvidenceSignal>): EvidenceSignal => ({
  kind: "usage", key: "k", label: "L", before: 0.5, after: 0.6, unit: "share", weeks_before: [3, 4, 5], weeks_after: [6],
  source: "s", detail: null, ...over,
});

test("groupSignals separates recorded data from model components and keeps unknown kinds visible", () => {
  const e = { ...espn.risers[0].evidence!, signals: [sig({ kind: "usage" }), sig({ kind: "model" }), sig({ kind: "schedule" }), sig({ kind: "weather" as never })] };
  const g = groupSignals(e);
  assert.deepEqual(g.recorded.map((s) => s.kind), ["usage", "schedule", "weather"]);
  assert.deepEqual(g.model.map((s) => s.kind), ["model"]);
  assert.deepEqual(groupSignals(null), { recorded: [], model: [] });
});

test("signalValue formats by unit and never invents a value", () => {
  assert.equal(signalValue(0.62, "share"), "62%");
  assert.equal(signalValue(14.52, "count"), "14.5");
  assert.equal(signalValue(3, "count"), "3");
  assert.equal(signalValue(11.5, "points"), "11.5");
  assert.equal(signalValue("Questionable", "text"), "Questionable");
  assert.equal(signalValue(null, "share"), null);
  assert.equal(signalValue(Number.NaN, "count"), null);
});

test("signalSample names the weeks on each side", () => {
  assert.equal(signalSample({ weeks_before: [3, 4, 5], weeks_after: [6] }), "weeks 3–5 → week 6");
  assert.equal(signalSample({ weeks_before: [], weeks_after: [6] }), "week 6");
  assert.equal(signalSample({ weeks_before: [], weeks_after: [] }), "");
});

test("informationWindowText gives the dates and games the evidence was read from", () => {
  const e = espn.risers[0].evidence!;
  assert.equal(informationWindowText(e), "Oct 14 – Oct 15 · week 6: BUF");
  assert.equal(informationWindowText({ ...e, information_window: null }), null);
  assert.equal(informationWindowText({ ...e, information_window: { from: null, to: null, games: [] } }), null);
});

test("the fixture's evidence never claims a cause", () => {
  const banned = /\b(because|due to|led to|caused|thanks to)\b/i;
  for (const m of espn.risers.concat(espn.fallers)) {
    const e = m.evidence!;
    assert.ok(!banned.test(`${e.headline ?? ""} ${e.note} ${e.signals.map((s) => s.detail ?? "").join(" ")}`), m.name);
    assert.match(e.note, /Not shown to be the cause/);
  }
});

// ----------------------------------------------------------------------------- table

const sortMovers = (rows: readonly Mover[], sort: { key: string; dir: SortDir }) => sortRows(rows, (m) => moverSortValue(m, sort.key), sort.dir);

test("the table sort defaults to the size of the change and sorts any column both ways", () => {
  assert.equal(DEFAULT_SORT.key, "size");
  const names = (key: string, dir: "asc" | "desc") => sortMovers(espn.players, { key, dir }).map((m) => m.name.slice(-1));
  assert.deepEqual(names("size", "desc"), ["D", "B", "E", "A", "F", "C", "H", "G"]);
  assert.deepEqual(names("delta", "desc"), ["B", "A", "C", "H", "G", "F", "E", "D"]);
  assert.deepEqual(names("delta", "asc"), ["D", "E", "F", "G", "H", "C", "A", "B"]);
  assert.deepEqual(names("player", "asc"), ["A", "B", "C", "D", "E", "F", "G", "H"]);
  assert.equal(names("rank", "desc")[0], "B", "largest move up first");
  assert.ok(SORT_KEYS.includes("pct"));
});

test("the table sort puts a missing percent last in both directions", () => {
  const missing = { ...espn.players[0], pct: null, name: "Test Player Z" };
  const rows = [missing, ...espn.players.slice(1)];
  assert.equal(sortMovers(rows, { key: "pct", dir: "desc" }).at(-1)?.name, "Test Player Z");
  assert.equal(sortMovers(rows, { key: "pct", dir: "asc" }).at(-1)?.name, "Test Player Z");
});

// ----------------------------------------------------------------------------- variants

test("an own-model response carries components, horizons and a governance record that is not proof", () => {
  assert.equal(ours.experimental, true);
  assert.equal(ours.governance?.espn_superiority_established, false);
  const m = ours.risers[0];
  assert.ok(m.components);
  assert.ok(Math.abs(m.components.availability + m.components.per_game - 9) < 0.011);
  assert.ok(m.weeks.some((w) => w.horizon_before !== null));
});

test("an unavailable response has a reason and nothing to rank", () => {
  assert.equal(unavailable.available, false);
  assert.ok(unavailable.reason);
  assert.equal(chartMovers(unavailable).length, 0);
  assert.equal(windowText(unavailable.window.weeks), null);
});
