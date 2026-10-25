import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import {
  DASH, allRows, availabilityView, byeText, cleanId, compareHref, edgeMark, edgeText, espnProjectionSeries, formatEvidence,
  formatRowValue, isCompareResponse, leaderLabel, missingWhy, nextOpponentText, nextWeekOurs, opportunityPoints,
  oursProjectionSeries, parseCompareParams, projectionRows, projectionSourceLine, remainingProjectionChart, remainingWeeks,
  rowCell, scoringLabel, swapHref, unitLabel, unknownPlayerSide, upcomingText, usageRows, weekAxis, weeklyLeaguePoints,
  weeklyPointsChart, rowNote,
  type CompareResponse, type CompareRow,
} from "./compare.ts";

const fixture = JSON.parse(readFileSync(new URL("./fixtures/compare.synthetic.json", import.meta.url), "utf8")) as CompareResponse;
const fresh = (): CompareResponse => structuredClone(fixture);

/** The ESPN-source variant: no Ours rows, no Ours fields. */
function espnOnly(): CompareResponse {
  const r = fresh();
  r.source = "espn";
  r.rows = r.rows.filter((x) => x.key !== "next_week_ours" && x.key !== "ros_ours");
  for (const card of r.players) {
    const p = card.projection!;
    p.next_week!.ours = null;
    p.next_week!.ours_origin = null;
    p.ros!.ours = null;
    p.ros!.ours_available = null;
    p.ros!.ours_reason = null;
    for (const w of p.weeks) { w.ours = null; w.ours_origin = null; }
  }
  return r;
}

const row = (resp: CompareResponse, key: string): CompareRow => resp.rows.find((r) => r.key === key) as CompareRow;

// ---------------------------------------------------------------------------------- the fixture --

test("the synthetic fixture has the shape the page reads", () => {
  assert.ok(isCompareResponse(fixture));
  assert.equal(fixture.players.length, 2);
  assert.deepEqual(fixture.summary.map((s) => s.key), ["next_week", "stability", "ros", "trend"]);
  assert.equal(isCompareResponse({ players: [] }), false);
  assert.equal(isCompareResponse(null), false);
});

// ------------------------------------------------------------------------------------ URL state --

test("parseCompareParams accepts ids, negative D/ST ids and trims", () => {
  assert.deepEqual(parseCompareParams({ a: "4262921", b: "-16001" }), { a: 4262921, b: -16001, errors: [] });
  assert.deepEqual(parseCompareParams({ a: " 12 ", b: "  " }), { a: 12, b: null, errors: [] });
  assert.deepEqual(parseCompareParams({}), { a: null, b: null, errors: [] });
  assert.deepEqual(parseCompareParams(undefined), { a: null, b: null, errors: [] });
});

test("parseCompareParams rejects junk and names the side", () => {
  for (const junk of ["abc", "12abc", "1.5", "1e3", "--4", "+5", "0x10", "12 34", "<script>", "99999999999999999999"]) {
    const r = parseCompareParams({ a: "5", b: junk });
    assert.equal(r.a, 5, junk);
    assert.equal(r.b, null, junk);
    assert.equal(r.errors.length, 1, junk);
    assert.equal(r.errors[0].side, "b");
    assert.match(r.errors[0].message, /not a player id for B/);
  }
  const both = parseCompareParams({ a: "x", b: "y" });
  assert.deepEqual(both.errors.map((e) => e.side), ["a", "b"]);
  const repeated = parseCompareParams({ a: ["1", "2"] });
  assert.equal(repeated.a, null);
  assert.equal(repeated.errors[0].side, "a");
});

test("a long junk value is shortened in the message, never echoed in full", () => {
  const r = parseCompareParams({ a: "x".repeat(500) });
  assert.ok(r.errors[0].message.length < 200);
});

test("compareHref builds the shareable URL and leaves out a missing side", () => {
  assert.equal(compareHref(1, 2), "/compare?a=1&b=2");
  assert.equal(compareHref(1), "/compare?a=1");
  assert.equal(compareHref(1, null), "/compare?a=1");
  assert.equal(compareHref(undefined, 2), "/compare?b=2");
  assert.equal(compareHref(), "/compare");
  assert.equal(compareHref(-16001, "4262921"), "/compare?a=-16001&b=4262921");
  assert.equal(compareHref("junk", 2), "/compare?b=2");
});

test("swapHref exchanges the sides", () => {
  assert.equal(swapHref(1, 2), "/compare?a=2&b=1");
  assert.equal(swapHref(1), "/compare?b=1");
  assert.equal(swapHref(), "/compare");
});

test("URL round trip: compareHref then parseCompareParams gives the same ids, negatives included", () => {
  const cases: [number | null, number | null][] = [[1, 2], [-16001, 4262921], [4262921, -16012], [7, null], [null, 7], [null, null], [5, 5]];
  for (const [a, b] of cases) {
    const href = compareHref(a, b);
    const q = new URL(href, "http://x").searchParams;
    const parsed = parseCompareParams(Object.fromEntries(q.entries()));
    assert.deepEqual({ a: parsed.a, b: parsed.b, errors: parsed.errors }, { a, b, errors: [] }, href);
  }
  const once = new URL(swapHref(3, -4), "http://x").searchParams;
  const swapped = parseCompareParams(Object.fromEntries(once.entries()));
  assert.deepEqual([swapped.a, swapped.b], [-4, 3]);
});

test("cleanId normalises and refuses", () => {
  assert.equal(cleanId("-0"), 0);
  assert.equal(cleanId(" 42 "), 42);
  assert.equal(cleanId(3.5), null);
  assert.equal(cleanId(null), null);
});

test("unknownPlayerSide reads the side out of the backend's 404", () => {
  assert.deepEqual(unknownPlayerSide("Unknown ESPN player id 123 (b)"), { side: "b", id: 123 });
  assert.deepEqual(unknownPlayerSide("Unknown ESPN player id -16001 (a)"), { side: "a", id: -16001 });
  assert.equal(unknownPlayerSide("No league synced"), null);
  assert.equal(unknownPlayerSide(undefined), null);
});

// ----------------------------------------------------------------------------- unit formatting --

test("formatRowValue: each unit prints in its own scale", () => {
  assert.equal(formatRowValue("share", 0.35), "35.0%");
  assert.equal(formatRowValue("pts", 14.25), "14.3");
  assert.equal(formatRowValue("pts_per_game", 15.04), "15.0");
  assert.equal(formatRowValue("per_game", 8), "8.0");
  assert.equal(formatRowValue("yards_per_game", 95), "95.0");
  assert.equal(formatRowValue("games", 10), "10");
  assert.equal(formatRowValue("games", 9.6), "10");
  assert.equal(formatRowValue("trend", 0.32), "+0.32");
  assert.equal(formatRowValue("trend", -0.05), "−0.05");
  assert.equal(formatRowValue("trend", 0), "0.00");
  assert.equal(formatRowValue("trend", -0.001), "0.00");
});

test("formatRowValue: a rating always carries its own scale; a 0-10 value is never /100", () => {
  assert.equal(formatRowValue("rating", 87.4, { scale: 100, legacy: false }), "87.4/100");
  assert.equal(formatRowValue("rating", 8.7, { scale: 10, legacy: true }), "8.7/10 (legacy)");
  assert.equal(formatRowValue("rating", 8.7, { scale: 10 }), "8.7/10 (legacy)");
  assert.ok(!formatRowValue("rating", 8.7, { scale: 10, legacy: true }).includes("/100"));
  // No scale known: the bare number, never an invented denominator.
  assert.equal(formatRowValue("rating", 8.7), "8.7");
});

test("a missing value is a dash for every unit, never a zero", () => {
  for (const unit of ["pts", "pts_per_game", "per_game", "yards_per_game", "share", "games", "trend", "rating"] as const) {
    assert.equal(formatRowValue(unit, null), DASH, unit);
    assert.equal(formatRowValue(unit, undefined), DASH, unit);
    assert.equal(formatRowValue(unit, Number.NaN), DASH, unit);
  }
  assert.equal(formatRowValue("pts", 0), "0.0"); // a real zero still prints
});

test("unitLabel: points rows name league scoring, nflverse PPR says so", () => {
  const scoring = { label: "Half PPR (0.5 pt per reception)" };
  assert.match(unitLabel("pts", scoring), /Half PPR \(0\.5 pt per reception\)/);
  assert.match(unitLabel("pts_per_game", scoring, "league_pts_pg"), /Half PPR/);
  assert.equal(unitLabel("pts_per_game", scoring, "ppr_pg"), "PPR (nflverse)");
  assert.match(unitLabel("pts_per_game", scoring, "xfp_pg"), /PPR \(nflverse\)/);
  assert.equal(unitLabel("share", scoring), "Share of team");
  // The label never claims a scale: legacy 0–10 values and 0–100 ratings carry their own "/10 (legacy)" or "/100".
  assert.doesNotMatch(unitLabel("rating", scoring), /0–100|0–10/);
});

test("missing scoring settings: label falls back, nothing throws", () => {
  assert.equal(scoringLabel(undefined), "League scoring");
  assert.equal(scoringLabel(null), "League scoring");
  assert.equal(scoringLabel({ label: "  " }), "League scoring");
  assert.equal(scoringLabel({ label: "League scoring (settings not synced)" }), "League scoring (settings not synced)");
  assert.match(unitLabel("pts", null), /League scoring/);
  assert.match(unitLabel("pts_per_game", undefined, "league_pts_pg"), /League scoring/);
  // The response-level case: settings not synced.
  const r = fresh();
  r.scoring = { scoring_type: null, reception_points: null, settings_synced: false, label: "League scoring (settings not synced)", note: "n" };
  assert.match(allRows(r).find((x) => x.key === "next_week_espn")!.unit, /League scoring \(settings not synced\)/);
});

test("projectionSourceLine names the source in use", () => {
  assert.equal(projectionSourceLine("espn"), "Projections: ESPN");
  assert.equal(projectionSourceLine(undefined), "Projections: ESPN");
  assert.equal(projectionSourceLine("independent"), "Projections: ESPN + Ours");
  assert.equal(projectionSourceLine("ours"), "Projections: ESPN + Ours");
});

// ------------------------------------------------------------------------------------- edges --

test("edge helpers: marker plus accessible text", () => {
  assert.equal(edgeMark("a"), "◀");
  assert.equal(edgeMark("b"), "▶");
  assert.equal(edgeMark("even"), "=");
  assert.equal(edgeMark("n/a"), "–");
  assert.equal(edgeText("a", "Synthetic WR A", "Synthetic WR B"), "Edge: Synthetic WR A");
  assert.equal(edgeText("b", "Synthetic WR A", "Synthetic WR B"), "Edge: Synthetic WR B");
  assert.equal(edgeText("even", "A", "B"), "Even");
  assert.equal(edgeText("n/a", "A", "B"), "Not comparable");
});

test("leaderLabel names the leader or says there is no call", () => {
  assert.equal(leaderLabel("a", "X", "Y"), "X");
  assert.equal(leaderLabel("b", "X", "Y"), "Y");
  assert.equal(leaderLabel("even", "X", "Y"), "Even");
  assert.equal(leaderLabel(null, "X", "Y"), "No call");
});

// --------------------------------------------------------------------- identical player twice --

test("identical player twice: every row is n/a and prints as Not comparable", () => {
  const r = fresh();
  r.identical = true;
  r.players = [r.players[0], structuredClone(r.players[0])];
  r.rows = r.rows.map((x) => ({ ...x, b: x.a, edge: "n/a" as const }));
  assert.ok(allRows(r).every((x) => x.edge === "n/a"));
  assert.ok(allRows(r).every((x) => edgeText(x.edge, "A", "B") === "Not comparable"));
  // Both sides render the same cell.
  assert.deepEqual(rowCell(r, r.rows[0], "a"), rowCell(r, r.rows[0], "b"));
});

// ---------------------------------------------------------------------- different positions --

test("different positions: a non-applicable row prints a dash with its why", () => {
  const r = fresh();
  r.same_position = false;
  const [a, b] = r.players;
  b.player.position = "RB";
  b.usage.metrics.carries_pg = { value: 14.5, applicable: true, why: null };
  b.usage.metrics.air_yards_pg = { value: null, applicable: false, why: "Air yards describe downfield receiving roles; not meaningful for a running back." };
  const carries = row(r, "carries_pg");
  carries.b = 14.5;
  const air = row(r, "air_yards_pg");
  air.b = null;
  air.edge = "n/a";
  assert.deepEqual(rowCell(r, carries, "a"), { text: DASH, missing: true, why: "Carries are not a receiver role measure." });
  assert.equal(rowCell(r, carries, "b").text, "14.5");
  const airB = rowCell(r, air, "b");
  assert.equal(airB.text, DASH);
  assert.equal(airB.missing, true);
  assert.match(airB.why!, /not meaningful for a running back/);
  assert.equal(rowCell(r, air, "a").text, "95.0");
  assert.equal(a.player.position, "WR");
});

test("not-applicable metric with no why still explains itself", () => {
  const r = fresh();
  r.players[0].usage.metrics.snap_share = { value: null, applicable: false, why: null };
  const why = missingWhy(r.players[0], row(r, "snap_share"));
  assert.match(why, /Not applicable to WR/);
});

test("kickers and defenses: usage rows are dashes with the backend's reason, league points still print", () => {
  const r = fresh();
  const why = "No NFL usage data for kickers and team defenses.";
  for (const card of r.players) {
    card.player.position = "K";
    for (const k of Object.keys(card.usage.metrics)) card.usage.metrics[k] = { value: null, applicable: false, why };
    card.usage.metrics.league_pts_pg = { value: 8.4, applicable: true, why: null };
  }
  for (const x of r.rows) if (x.group !== "projection") { x.a = null; x.b = null; x.edge = "n/a"; }
  row(r, "league_pts_pg").a = 8.4;
  row(r, "league_pts_pg").b = 8.4;
  const snap = usageRows(r).find((x) => x.key === "snap_share")!;
  assert.equal(snap.a.text, DASH);
  assert.equal(snap.a.why, why);
  assert.equal(usageRows(r).find((x) => x.key === "league_pts_pg")!.a.text, "8.4");
});

// ----------------------------------------------------------------------- missing source data --

test("missing projections and usage: dashes with reasons, never 0", () => {
  const r = fresh();
  const [a] = r.players;
  a.projection = null;
  row(r, "next_week_espn").a = null;
  row(r, "ros_espn").a = null;
  row(r, "games_remaining").a = null;
  a.usage.metrics.snap_share = { value: null, applicable: true, why: "No usage record" };
  row(r, "snap_share").a = null;
  row(r, "opportunity_rating").a = null;
  a.usage.ratings.opportunity = null;

  for (const key of ["next_week_espn", "ros_espn", "games_remaining", "snap_share", "opportunity_rating"]) {
    const c = rowCell(r, row(r, key), "a");
    assert.equal(c.text, DASH, key);
    assert.equal(c.missing, true, key);
    assert.ok(c.why && c.why.length > 0, key);
    assert.notEqual(c.text, "0.0", key);
  }
  assert.equal(rowCell(r, row(r, "snap_share"), "a").why, "No usage record");
  assert.match(rowCell(r, row(r, "next_week_espn"), "a").why!, /projection window is unknown/);
  // The other player is untouched.
  assert.equal(rowCell(r, row(r, "next_week_espn"), "b").text, "12.1");
});

test("ESPN projects only some remaining weeks: the ROS dash says how many", () => {
  const r = fresh();
  const card = r.players[1];
  card.projection!.ros!.espn = null;
  card.projection!.ros!.espn_weeks_available = 8;
  card.projection!.ros!.espn_weeks_expected = 10;
  row(r, "ros_espn").b = null;
  assert.match(rowCell(r, row(r, "ros_espn"), "b").why!, /8 of 10 remaining weeks/);
});

test("a bye week next week is a dash with the week, not a 0", () => {
  const r = fresh();
  const nw = r.players[0].projection!.next_week!;
  nw.on_bye = true;
  nw.opponent = null;
  nw.espn = null;
  nw.ours = null;
  nw.ours_origin = "bye";
  row(r, "next_week_espn").a = null;
  row(r, "next_week_ours").a = null;
  const e = rowCell(r, row(r, "next_week_espn"), "a");
  assert.equal(e.text, DASH);
  assert.equal(e.tag, "bye");
  assert.equal(e.why, "On bye in week 7.");
  assert.equal(rowCell(r, row(r, "next_week_ours"), "a").tag, "bye");
  assert.equal(nextOpponentText(r.players[0]), "Wk 7 BYE");
});

// ---------------------------------------------------------------------------------- horizons --

test("next-week Ours is absent when its origin is ESPN, and says why", () => {
  const r = fresh();
  const b = rowCell(r, row(r, "next_week_ours"), "b");
  assert.equal(b.text, DASH);
  assert.equal(b.tag, "espn_fallback");
  assert.match(b.why!, /No independent estimate/);
  assert.deepEqual(nextWeekOurs(r.players[1]), { kind: "espn", value: null });
  assert.deepEqual(nextWeekOurs(r.players[0]), { kind: "ours", value: 13 });
  // Even if the payload carried a number alongside origin espn, it is not shown as Ours.
  r.players[1].projection!.next_week!.ours = 99;
  row(r, "next_week_ours").b = 99;
  assert.equal(rowCell(r, row(r, "next_week_ours"), "b").text, DASH);
});

test("our own 0 for an unavailable player is shown and tagged, a bye 0 is not", () => {
  const r = fresh();
  const nw = r.players[0].projection!.next_week!;
  nw.ours = 0;
  nw.ours_origin = "ours_zero";
  row(r, "next_week_ours").a = 0;
  const c = rowCell(r, row(r, "next_week_ours"), "a");
  assert.equal(c.text, "0.0");
  assert.equal(c.tag, "zero");
  assert.deepEqual(nextWeekOurs(r.players[0]), { kind: "zero", value: 0 });
  nw.on_bye = true;
  assert.equal(rowCell(r, row(r, "next_week_ours"), "a").text, DASH);
});

test("the Ours rest-of-season row is experimental, with its reason when it has no total", () => {
  const rows = projectionRows(fixture);
  const ros = rows.find((x) => x.key === "ros_ours")!;
  assert.equal(ros.experimental, true);
  assert.equal(rows.find((x) => x.key === "ros_espn")!.experimental, false);
  assert.equal(ros.a.text, formatRowValue("pts", row(fixture, "ros_ours").a));
  assert.equal(ros.b.text, DASH);
  assert.match(ros.b.why!, /9 of 10 remaining weeks/);
});

test("the ESPN source has no Ours rows at all", () => {
  const r = espnOnly();
  assert.deepEqual(projectionRows(r).map((x) => x.key), ["next_week_espn", "ros_espn", "games_remaining"]);
  assert.equal(remainingProjectionChart(r)!.ours, null);
});

test("projection rows and usage rows partition the table", () => {
  const p = projectionRows(fixture).map((x) => x.key);
  const u = usageRows(fixture).map((x) => x.key);
  assert.equal(p.length + u.length, fixture.rows.length);
  assert.deepEqual(p, ["next_week_espn", "next_week_ours", "ros_espn", "ros_ours", "games_remaining"]);
  assert.equal(u[0], "snap_share");
  assert.deepEqual(u.slice(-3), ["trend_score", "opportunity_rating", "production_rating"]);
});

// ------------------------------------------------------------------------- ratings and scale --

test("ratings print with their own scale in the row cells", () => {
  const r = fresh();
  assert.equal(rowCell(r, row(r, "opportunity_rating"), "a").text, "82.5/100");
  r.players[1].usage.ratings.opportunity = { value: 7.1, scale: 10, legacy: true };
  row(r, "opportunity_rating").b = 7.1;
  row(r, "opportunity_rating").edge = "n/a";
  const legacy = rowCell(r, row(r, "opportunity_rating"), "b");
  assert.equal(legacy.text, "7.1/10 (legacy)");
  assert.ok(!legacy.text.includes("/100"));
});

test("other units in the row cells: shares as percent, trend signed, games integer", () => {
  assert.equal(rowCell(fixture, row(fixture, "snap_share"), "a").text, "82.0%");
  assert.equal(rowCell(fixture, row(fixture, "trend_score"), "b").text, "−0.05");
  assert.equal(rowCell(fixture, row(fixture, "games_remaining"), "a").text, "10");
  assert.equal(rowCell(fixture, row(fixture, "air_yards_pg"), "a").text, "95.0");
});

// --------------------------------------------------------------------------- weekly (bye gaps) --

test("weekAxis is 1..n and refuses nonsense", () => {
  assert.deepEqual(weekAxis(6), [1, 2, 3, 4, 5, 6]);
  assert.deepEqual(weekAxis(0), []);
  assert.deepEqual(weekAxis(-3), []);
  assert.deepEqual(weekAxis(null), []);
  assert.deepEqual(weekAxis(Number.NaN), []);
  assert.equal(weekAxis(500).length, 30);
});

test("weekly league points align on weeks 1..completed with null gaps for byes and absent weeks", () => {
  const chart = weeklyPointsChart(fixture)!;
  assert.deepEqual(chart.weeks, [1, 2, 3, 4, 5, 6]);
  assert.deepEqual(chart.xLabels, ["1", "2", "3", "4", "5", "6"]);
  const [a, b] = chart.series;
  assert.deepEqual(a.points, [18.4, 12.1, 21, 9.5, null, 18]); // bye in week 5
  assert.deepEqual(b.points, [8.2, 14.6, null, 5.9, 16.8, 12.9]); // no game score in week 3
  assert.ok(a.points.every((p) => p !== 0));
  assert.equal(a.label, "Synthetic WR A");
});

test("a real 0-point week is kept; a null score is a gap", () => {
  const r = fresh();
  r.players[0].weekly[0].league_points = 0;
  r.players[0].weekly[1].league_points = null;
  assert.deepEqual(weeklyLeaguePoints(r.players[0], [1, 2, 3]).slice(0, 3), [0, null, 21]);
});

test("no completed weeks or no points at all: no weekly chart", () => {
  const r = fresh();
  r.completed_through_week = 0;
  assert.equal(weeklyPointsChart(r), null);
  const s = fresh();
  for (const c of s.players) c.weekly = [];
  assert.equal(weeklyPointsChart(s), null);
});

// ------------------------------------------------------------ remaining-season projections --

test("remaining weeks are the union of both players' weeks, ascending", () => {
  const r = fresh();
  r.players[0].projection!.weeks = r.players[0].projection!.weeks.filter((w) => w.week <= 12);
  r.players[1].projection!.weeks = r.players[1].projection!.weeks.filter((w) => w.week >= 10);
  assert.deepEqual(remainingWeeks(r), [7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17]);
  const p = fresh();
  p.players[0].projection = null;
  p.players[1].projection = null;
  assert.deepEqual(remainingWeeks(p), []);
  assert.equal(remainingProjectionChart(p), null);
});

test("ESPN projection series: byes are gaps, and a week one player lacks is a gap for that player only", () => {
  const chart = remainingProjectionChart(fixture)!;
  assert.deepEqual(chart.weeks, [7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17]);
  const [a, b] = chart.espn;
  assert.equal(a.points[2], null); // A's bye, week 9
  assert.equal(b.points[2], 12.5); // B plays week 9
  assert.equal(b.points[4], null); // B's bye, week 11
  assert.equal(a.points[4], 14.4);
  assert.equal(a.points[0], 14.2);
  const r = fresh();
  r.players[1].projection!.weeks = r.players[1].projection!.weeks.filter((w) => w.week !== 8);
  assert.equal(remainingProjectionChart(r)!.espn[1].points[1], null);
  assert.equal(remainingProjectionChart(r)!.weeks.length, 11);
});

test("Ours series only carries origin ours or ours_zero numbers; the ESPN source has none", () => {
  const chart = remainingProjectionChart(fixture)!;
  assert.ok(chart.ours);
  const [a, b] = chart.ours!;
  assert.equal(a.points[0], 13.0);
  assert.equal(a.points[2], null); // bye
  assert.equal(b.points[0], null); // origin espn: not an Ours number
  assert.equal(b.points[1], 11.4); // origin ours
  assert.equal(b.points.filter((p) => p !== null).length, 1);

  const r = fresh();
  r.players[0].projection!.weeks[0].ours_origin = "espn";
  r.players[0].projection!.weeks[0].ours = 5; // never plotted
  assert.equal(oursProjectionSeries(r.players[0], [7])[0], null);
  r.players[0].projection!.weeks[1].ours_origin = "ours_zero";
  r.players[0].projection!.weeks[1].ours = 0;
  assert.equal(oursProjectionSeries(r.players[0], [8])[0], 0);
  r.players[0].projection!.weeks[2].ours_origin = "ours_zero"; // a bye
  r.players[0].projection!.weeks[2].ours = 0;
  assert.equal(oursProjectionSeries(r.players[0], [9])[0], null);

  assert.equal(remainingProjectionChart(espnOnly())!.ours, null);
});

test("Ours chart is dropped when no Ours number exists at all", () => {
  const r = fresh();
  for (const c of r.players) for (const w of c.projection!.weeks) { w.ours = null; w.ours_origin = "espn"; }
  assert.equal(remainingProjectionChart(r)!.ours, null);
});

test("espnProjectionSeries leaves a missing projection as a gap", () => {
  const r = fresh();
  r.players[0].projection!.weeks[1].espn = null;
  assert.equal(espnProjectionSeries(r.players[0], [8])[0], null);
});

// ------------------------------------------------------------------ opportunity vs production --

test("opportunity vs production: xFP/g against PPR/g, one point per player", () => {
  const pts = opportunityPoints(fixture);
  assert.deepEqual(pts, [
    { id: "a", label: "Synthetic WR A", x: 15.1, y: 16 },
    { id: "b", label: "Synthetic WR B", x: 13, y: 11.7 },
  ]);
});

test("a point needs both xFP/g and PPR/g; unmatched or kicker players contribute none", () => {
  const r = fresh();
  r.players[0].usage.metrics.xfp_pg = { value: null, applicable: true, why: "No usage record" };
  assert.deepEqual(opportunityPoints(r).map((p) => p.id), ["b"]);
  r.players[1].usage.metrics.ppr_pg = { value: null, applicable: false, why: "No NFL usage data for kickers and team defenses." };
  assert.deepEqual(opportunityPoints(r), []);
});

// -------------------------------------------------------------------- context cells and text --

test("context cells: opponents, bye weeks, upcoming schedule", () => {
  const [a, b] = fixture.players;
  assert.equal(nextOpponentText(a), "Wk 7 vs BUF");
  assert.equal(nextOpponentText(b), "Wk 7 @TB");
  assert.equal(byeText(a), "Week 9");
  assert.equal(byeText({ ...a, projection: { ...a.projection!, bye_weeks: [9, 12] } }), "Weeks 9, 12");
  assert.equal(byeText({ ...a, projection: null }), null);
  assert.equal(upcomingText(a, 3), "7 vs BUF · 8 @KC · 9 BYE");
  assert.equal(upcomingText({ ...a, projection: null }), null);
  assert.equal(nextOpponentText({ ...a, projection: null }), null);
});

test("availability view: current report, stale report, none", () => {
  const [a, b] = fixture.players;
  const none = availabilityView(a, 7);
  assert.equal(none.label, "No designation reported");
  assert.equal(none.currency, "none");
  assert.match(none.note, /not confirmation/);
  assert.equal(none.report, null);

  const cur = availabilityView(b, 7);
  assert.equal(cur.currency, "current");
  assert.match(cur.currencyText, /week 7/);
  assert.equal(cur.report, "Questionable · Hamstring · practice: Limited");

  const stale = structuredClone(b);
  stale.availability.report_is_current = false;
  stale.availability.nfl_report!.week = 5;
  const s = availabilityView(stale, 7);
  assert.equal(s.currency, "stale");
  assert.match(s.currencyText, /week 5 report, this week is 7/);

  stale.availability.report_is_current = null;
  assert.equal(availabilityView(stale, 7).currency, "unknown");
});

test("role evidence reads as a sentence in the right unit", () => {
  const [a, b] = fixture.players;
  assert.equal(formatEvidence(a.role.evidence[0]), "Target share 22.0% → 31.0% (+9.0 pts)");
  assert.equal(formatEvidence(a.role.evidence[1]), "Targets 8.3 → 8.0 per game (−0.3)");
  assert.equal(formatEvidence(b.role.evidence[0]), "Snap share 78.0% → 71.0% (−7.0 pts)");
  assert.equal(
    formatEvidence({ metric: "carries", label: "UP", baseline_avg: null, recent_avg: 5, delta: null, unit: "per_game", baseline_weeks: [], recent_weeks: [] }),
    "Carries — → 5.0 per game",
  );
});

test("findings carry no verdict wording", () => {
  const banned = /win probab|better trade|trade asset|you should|must (start|trade)/i;
  for (const f of fixture.summary) assert.ok(!banned.test(`${f.title} ${f.text} ${f.basis}`), f.key);
  assert.ok(!banned.test(fixture.disclaimer));
});

test("allRows keeps the backend's order and labels every row", () => {
  const rows = allRows(fixture);
  assert.deepEqual(rows.map((x) => x.key), fixture.rows.map((x) => x.key));
  for (const x of rows) assert.ok(x.label && x.unit, x.key);
});

test("rowNote drops not-applicable parts (the cells carry the reason) and keeps the rest", () => {
  assert.equal(rowNote("Not applicable for Josh Allen (QB)"), null);
  assert.equal(rowNote("Not applicable for A (QB); Fewer than 3 games for B"), "Fewer than 3 games for B");
  assert.equal(rowNote(null), null);
  assert.equal(rowNote("Position-relative ratings: not equal value"), "Position-relative ratings: not equal value");
});

test("a league without ESPN projections (Sleeper) drops the ESPN rows and says Ours only", () => {
  const r = fresh();
  const keys = projectionRows(r, false).map((x) => x.key);
  assert.ok(!keys.includes("next_week_espn") && !keys.includes("ros_espn"));
  assert.deepEqual(projectionRows(r, true).map((x) => x.key), projectionRows(r).map((x) => x.key));
  assert.ok(!allRows(r, false).some((x) => x.key === "next_week_espn" || x.key === "ros_espn"));
  assert.equal(allRows(r, false).length, allRows(r).length - allRows(r).filter((x) => x.key === "next_week_espn" || x.key === "ros_espn").length);
  assert.equal(projectionSourceLine("independent", false), "Projections: Ours");
  assert.equal(projectionSourceLine("independent", true), "Projections: ESPN + Ours");
});
