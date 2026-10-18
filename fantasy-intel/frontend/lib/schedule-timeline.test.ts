import assert from "node:assert/strict";
import test from "node:test";
import { buildColumns, buildTimeline, countNextByLabel, describeWeek, nextWeekOf, phaseGroups } from "./schedule-timeline.ts";
import type { SiPlayer, SiWeek } from "./types.ts";

const week = (over: Partial<SiWeek> & { week: number }): SiWeek => ({
  phase: "regular", matchup_period: over.week, actionable: true, status: "scheduled", opponent: "GB", home: true,
  label: "middle", label_text: "Middle of the league", ...over,
});

const player = (weeks: SiWeek[]): SiPlayer => ({
  espn_player_id: 1, name: "A", position: "QB", pro_team: "DET", injury_status: null, on_ir: false,
  identity: { gsis_id: null, match_confidence: "high", nflverse_latest_team: null, nflverse_latest_week: null },
  notes: [], weeks,
  summary: {} as SiPlayer["summary"],
});

test("buildColumns marks the first playoff week only", () => {
  const cols = buildColumns([13, 14, 15, 16], [15, 16], { "15": 15 }, 14);
  assert.deepEqual(cols.map((c) => c.phase), ["regular", "regular", "playoffs", "playoffs"]);
  assert.deepEqual(cols.map((c) => c.startsPlayoffs), [false, false, true, false]);
  assert.equal(cols[2].matchupPeriod, 15);
  assert.equal(cols[0].matchupPeriod, null);
});

test("buildColumns with no playoff weeks never starts playoffs", () => {
  assert.ok(buildColumns([1, 2], [], {}).every((c) => !c.startsPlayoffs));
});

test("buildColumns uses regular_season_through and leaves unestablished phases null", () => {
  const cols = buildColumns([13, 14, 15, 16], [15, 16], {}, 14);
  assert.deepEqual(cols.map((c) => c.phase), ["regular", "regular", "playoffs", "playoffs"]);
  // The settings establish nothing: no boundary and no playoff weeks.
  const unknown = buildColumns([6, 7, 8], [], {}, null);
  assert.deepEqual(unknown.map((c) => c.phase), [null, null, null]);
  // Playoff weeks listed but no regular-season boundary: only the listed weeks are claimed.
  assert.deepEqual(buildColumns([13, 14, 15], [15], {}, null).map((c) => c.phase), [null, null, "playoffs"]);
});

test("phaseGroups groups consecutive phases and labels an unknown phase as such", () => {
  const groups = phaseGroups(buildColumns([13, 14, 15, 16], [15, 16], {}, 14));
  assert.deepEqual(groups.map((g) => [g.label, g.count]), [["Regular season", 2], ["Fantasy playoffs", 2]]);
  const unknown = phaseGroups(buildColumns([6, 7, 8], [], {}, null));
  assert.deepEqual(unknown.map((g) => [g.phase, g.label, g.count]), [[null, "Phase unknown", 3]]);
});

test("a null phase on a week passes through to its cell", () => {
  const w = week({ week: 7, phase: null });
  const cell = buildTimeline([7], player([w]))[0];
  assert.equal(cell.phase, null);
});

test("buildTimeline covers bye, away, home and missing weeks", () => {
  const p = player([
    week({ week: 6, status: "bye", opponent: null, label: "bye", label_text: "Bye" }),
    week({ week: 7, home: false, opponent: "MIN" }),
  ]);
  const cells = buildTimeline([6, 7, 8], p);
  assert.deepEqual(cells.map((c) => c.kind), ["bye", "game", "none"]);
  assert.equal(cells[0].where, null);
  assert.equal(cells[1].where, "@");
  assert.equal(cells[1].opponent, "MIN");
  assert.equal(cells[2].label, "unavailable");
});

test("describeWeek includes the rank when evidenced", () => {
  const w = week({
    week: 9, home: false, opponent: "MIA", label: "permissive", label_text: "Observed more permissive",
    evidence: {
      defteam: "MIA", games_before_week: 9, through_week: 5, games: 5, weeks_sampled: [], excluded_weeks: [], excluded_reasons: {},
      primary_metric: "pass_epa_per_play", label: "permissive", label_text: "", sentence: "",
      metrics: [{ metric: "pass_epa_per_play", label: "", value: 0.1, numerator: 1, denominator: 10, rank: 3, ranked_defenses: 32, band: "permissive", missing_reason: null }],
    },
  });
  assert.equal(describeWeek(w), "Week 9: away at MIA, observed more permissive, opponent rank 3 of 32");
});

test("countNextByLabel counts each player's first actionable week", () => {
  const a = player([week({ week: 5, actionable: false, label: "tougher" }), week({ week: 6, label: "permissive" })]);
  const b = player([week({ week: 6, label: "permissive" })]);
  const c = player([week({ week: 6, status: "bye", label: "bye", opponent: null })]);
  const counts = countNextByLabel([a, b, c]);
  assert.equal(counts.permissive, 2);
  assert.equal(counts.tougher, 0);
  assert.equal(counts.bye, 1);
  assert.equal(nextWeekOf(a)?.week, 6);
});
