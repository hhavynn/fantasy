import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  DECISION_TEXT, POSITIONS, VERDICT_BADGE, VERDICT_TEXT, exclusionLabel, exclusionRows, headline, intervalRows,
  intervalText, leadGapText, rbVolumeLabel, rbVolumeRows, signedNum, weeklySeries, weeksText,
  type PerformanceResponse,
} from "./projection-performance.ts";

// The synthetic fixture is the backend's own output for its synthetic test world (regenerated and compared by
// backend/tests/test_projection_performance.py), so these checks hold the page to the numbers the backend serves.
const fixture = JSON.parse(readFileSync(new URL("./fixtures/projection-performance.synthetic.json", import.meta.url), "utf8")) as PerformanceResponse;
const report = fixture.report!;

const empty: PerformanceResponse = {
  schema_version: "projection-performance-api/v1", season: 2026, available: false, status: "no_report",
  unavailable_reason: "No projection-performance report has been published for this season yet.", report: null,
  report_sha256: null, stale: false, stale_reasons: [], rejected: [], governance: fixture.governance,
};

test("the fixture is labelled synthetic and never looks like a real result", () => {
  assert.equal(report.synthetic, true);
  assert.match(report.label ?? "", /SYNTHETIC/);
});

test("frontend verdict text matches the backend statement for every position and comparison", () => {
  for (const pos of POSITIONS) {
    const p = report.positions[pos];
    assert.equal(VERDICT_TEXT[p.verdict], p.statement, pos);
    assert.equal(VERDICT_TEXT[p.conditional.verdict], p.conditional.statement, `${pos} conditional`);
  }
  for (const m of Object.values(report.start_sit.models)) {
    for (const s of [m.same_position, m.flex, m.close_calls, m.lineups]) {
      if (s) assert.equal(DECISION_TEXT[s.verdict], s.statement);
    }
  }
});

test("the interval chart shows the published point and bounds unchanged, muting insufficient positions", () => {
  const rows = intervalRows(report);
  assert.deepEqual(rows.map((r) => r.label), [...POSITIONS]);
  for (const r of rows) {
    const p = report.positions[r.label as (typeof POSITIONS)[number]];
    assert.equal(r.value, p.mae_diff);
    assert.deepEqual([r.lo, r.hi], p.ci95 ?? [null, null]);
    assert.equal(r.n, p.paired_n);
    assert.equal(r.muted, p.verdict === "insufficient_evidence");
  }
  // TE has a negative difference on too few weeks: it must stay muted and never read as a win
  const te = rows.find((r) => r.label === "TE")!;
  assert.ok(te.value! < 0 && te.muted && te.verdict === "insufficient_evidence");
  assert.equal(VERDICT_BADGE[te.verdict].tone, "");
});

test("a verdict in either direction carries the same visual weight", () => {
  assert.equal(VERDICT_BADGE.ours_better.tone, "good");
  assert.equal(VERDICT_BADGE.espn_better.tone, "bad");
  assert.equal(VERDICT_BADGE.no_significant_difference.tone, "");
  const qb = report.positions.QB;
  assert.equal(qb.verdict, "espn_better");
  assert.ok(qb.ci95![0] > 0);
  const rb = report.positions.RB;
  assert.equal(rb.verdict, "no_significant_difference");
  assert.ok(rb.ci95![0] < 0 && rb.ci95![1] > 0);
});

test("weekly series add up to the paired observations and leave gaps for missing weeks", () => {
  for (const pos of POSITIONS) {
    const s = weeklySeries(report, pos);
    assert.deepEqual(s.weeks, report.weeks.fully_final);
    assert.equal(s.n.reduce((a, b) => a + b, 0), report.positions[pos].paired_n, pos);
  }
  const te = weeklySeries(report, "TE");
  assert.equal(te.ours[3], null);          // week 4: TE has no paired rows; a gap, not zero
  assert.ok(!report.weekly.some((w) => report.weeks.partial.includes(w.week)));
});

test("headline repeats the backend verdicts and keeps insufficient positions separate", () => {
  const h = headline(fixture);
  assert.equal(h.title, "Mixed results by position");
  assert.match(h.body, /more accurate than ESPN at WR/);
  assert.match(h.body, /less accurate than ESPN at QB/);
  assert.match(h.body, /no established difference at RB/);
  assert.match(h.body, /TE: insufficient prospective evidence/);
  const none = headline(empty);
  assert.equal(none.title, "No accuracy report yet");
  assert.equal(headline({ ...empty, status: "integrity_failed" }).title, "No verified report");
});

test("all-insufficient report says so and never implies a winner", () => {
  const all = JSON.parse(JSON.stringify(fixture)) as PerformanceResponse;
  for (const pos of POSITIONS) {
    all.report!.positions[pos].verdict = "insufficient_evidence";
  }
  all.report!.status.verdict = "insufficient_evidence";
  const h = headline(all);
  assert.equal(h.title, "Still collecting weeks");
  assert.equal(h.tone, "info");
  assert.match(h.body, /once a position clears/);
  assert.doesNotMatch(h.body, /more accurate/);
});

test("formatting helpers keep sign and missing values explicit", () => {
  assert.equal(signedNum(-0.4213), "−0.42");
  assert.equal(signedNum(1.1), "+1.10");
  assert.equal(signedNum(0.0001), "0.00");
  assert.equal(signedNum(null), null);
  assert.equal(intervalText([-2, 2]), "−2.00 to +2.00");
  assert.equal(intervalText(null), null);
  assert.equal(leadGapText(-24), "24 h later than ESPN");
  assert.equal(leadGapText(3.25), "3.3 h earlier than ESPN");
  assert.equal(leadGapText(0.2), "same time as ESPN");
  assert.equal(leadGapText(null), null);
  assert.equal(weeksText([1, 2, 3, 4, 5]), "1–5");
  assert.equal(weeksText([6, 7]), "6, 7");
  assert.equal(weeksText([2, 4, 5]), "2, 4, 5");
  assert.equal(weeksText([]), "none");
});

test("exclusion reasons are labelled in plain language and sorted by size", () => {
  const rows = exclusionRows(report.coverage.by_exclusion_reason);
  assert.ok(rows.length > 0);
  for (let i = 1; i < rows.length; i++) assert.ok(rows[i - 1].n >= rows[i].n);
  for (const r of rows) assert.doesNotMatch(r.label, /_/, r.code);
  assert.equal(exclusionLabel("week_not_fully_final"), "Week not fully final (preliminary)");
  assert.equal(exclusionLabel("some_new_reason"), "some new reason");
});

test("start/sit warnings and gates come straight from the report", () => {
  const ss = report.start_sit;
  assert.deepEqual(ss.gate, { min_decisions: 200, min_weeks: 8 });
  assert.ok(ss.warnings.some((w) => w.includes("a verdict needs at least 200 decisions and 8 weeks")));
  assert.equal(ss.models.ifp1_production.evaluated, false);
});

test("the published report carries no player, roster or league identifiers", () => {
  const text = JSON.stringify(fixture);
  for (const key of ["espn_player_id", "gsis_id", "league_id", "fantasy_team_id", "player_name"]) {
    assert.ok(!text.includes(`"${key}"`), key);
  }
});

test("the RB volume table shows the published groups unchanged and they add up to the RB rows", () => {
  const s = report.rb_volume_split!;
  for (const comparison of ["pure_independent", "conditional"] as const) {
    const rows = rbVolumeRows(report, comparison);
    assert.deepEqual(rows.map((r) => r.group), s.groups);
    const total = rows.reduce((a, r) => a + r.n, 0);
    assert.equal(total, comparison === "pure_independent" ? report.positions.RB.paired_n : report.positions.RB.conditional.n);
    for (const r of rows) {
      const g = s.comparisons[comparison].by_group[r.group];
      assert.equal(r.mae_diff, g.mae_diff);
      assert.deepEqual(r.ci95, g.ci95);
      assert.equal(VERDICT_TEXT[r.verdict], r.statement);
    }
  }
  assert.equal(rbVolumeLabel("low_volume", s.threshold), "Under 4 opportunities a game");
  assert.equal(rbVolumeLabel("volume", s.threshold), "4+ opportunities a game");
  // a report built before the split existed renders no table rather than inventing one
  assert.deepEqual(rbVolumeRows({ ...report, rb_volume_split: undefined }, "pure_independent"), []);
});
