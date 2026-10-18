import assert from "node:assert/strict";
import { test } from "node:test";
import { headline, lineText, splitDefenses, statusText, takeaways, type DstPlayer } from "./defenses.ts";

const dst = (id: number, team: string, status: string, exp: number | null): DstPlayer => ({
  espn_player_id: id, name: `${team} D/ST`, nfl_team: team, opponent: "NYG", is_home: true, fantasy_team_id: null,
  independent: { projected_points: exp, expected_points: exp, interval_80: null, coverage_status: status,
    stat_line: { sacks: 2.9, interceptions: 0.9, fumble_recoveries: 0.6, points_allowed: 17.8, yards_allowed: 301, p_any_td: 0.13 },
    context: { spread_line: 7, total_line: 41.5, opponent_implied_total: 17.25, expected_margin: 7, has_line: true },
    explanation: [{ feature: "headline", contribution: null, text: "NYG are expected to score 17.2 points." }],
    flags: [], missing_inputs: [], pregame: true, model_version: "dfp-r1" },
  espn: { projected_points: 7.6 }, comparison: { expected_minus_espn: null },
});

test("projected D/STs sort by expected points; byes and the rest follow by team", () => {
  const { projected, other } = splitDefenses([dst(1, "HOU", "projected", 8.7), dst(2, "GB", "bye", 0),
    dst(3, "CLE", "projected", 7.9), dst(4, "ATL", "no_estimate", null)]);
  assert.deepEqual(projected.map((p) => p.espn_player_id), [1, 3]);
  assert.deepEqual(other.map((p) => p.nfl_team), ["ATL", "GB"]);
});

test("the line reads from the defense's side, and is absent without one", () => {
  const p = dst(1, "HOU", "projected", 8.7);
  assert.equal(lineText(p.independent.context), "Favored by 7 · total 41.5");
  assert.equal(lineText({ ...p.independent.context!, expected_margin: -3.5 }), "3.5-pt underdog · total 41.5");
  assert.equal(lineText({ ...p.independent.context!, expected_margin: 0 }), "Pick'em · total 41.5");
  assert.equal(lineText({ spread_line: null, total_line: null, opponent_implied_total: null, expected_margin: null }), null);
  assert.equal(lineText(null), null);
});

test("takeaways, headline and status text", () => {
  const p = dst(1, "HOU", "projected", 8.7);
  assert.equal(takeaways(p.independent.stat_line)?.toFixed(1), "1.5");
  assert.equal(takeaways(null), null);
  assert.equal(headline(p), "NYG are expected to score 17.2 points.");
  assert.equal(statusText("bye"), "Bye week");
  assert.equal(statusText("something_else"), "something else");
});
