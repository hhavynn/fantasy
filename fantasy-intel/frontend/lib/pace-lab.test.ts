import assert from "node:assert/strict";
import { test } from "node:test";
import {
  PRESETS, backtestSentences, coverageText, mixText, paceApiPath, paceHref, parsePaceState, presetActive, resultText,
  seasonsText, type Backtest, type WeekRow,
} from "./pace-lab.ts";

test("the URL state keeps only well-formed values", () => {
  const s = parsePaceState({ season: "2025", seasons: "2", team: "lv", opp: "kc", window: "last4", through: "9", mc: "20", ml: "70", mt: "10" });
  assert.deepEqual(s, { season: 2025, seasons: 2, team: "LV", opp: "KC", window: "last4", through: 9, mix: [20, 70, 10] });
  const bad = parsePaceState({ season: "20x5", seasons: "7", team: "K1", opp: "KC", window: "last2", through: "19", ml: "50" });
  assert.deepEqual(bad, { season: null, seasons: 1, team: null, opp: null, window: "season", through: null, mix: null });
});

test("a mix needs a team and a positive weight; an opponent cannot be the team", () => {
  assert.equal(parsePaceState({ ml: "50" }).mix, null);
  assert.equal(parsePaceState({ team: "KC", mc: "0", ml: "0", mt: "0" }).mix, null);
  assert.deepEqual(parsePaceState({ team: "KC", ml: "50" }).mix, [0, 50, 0]);
  assert.equal(parsePaceState({ team: "KC", opp: "kc" }).opp, null);
  assert.equal(parsePaceState({ team: "KC", mc: "150" }).mix, null);
});

test("backend path and page links round-trip the state", () => {
  const s = parsePaceState({ team: "LV", opp: "KC", seasons: "2", mc: "20", ml: "70", mt: "10" });
  assert.equal(paceApiPath(s), "/api/pace-lab?seasons=2&team=LV&opponent=KC&mix_competitive=20&mix_leading=70&mix_trailing=10");
  assert.equal(paceApiPath(parsePaceState({})), "/api/pace-lab");
  const href = paceHref(s, { mix: null, opp: null });
  assert.equal(href, "/pace-lab?seasons=2&team=LV");
  const back = parsePaceState(Object.fromEntries(new URL(paceHref(s), "http://x").searchParams));
  assert.deepEqual(back, s);
  assert.equal(paceHref(s, { team: null }), "/pace-lab?seasons=2");
});

test("presets and wording", () => {
  assert.ok(presetActive(null, PRESETS[0]));
  assert.ok(presetActive([2, 7, 1], PRESETS[2]));
  assert.ok(!presetActive([20, 70, 10], PRESETS[3]));
  assert.equal(mixText([0.2, 0.7, 0.1]), "20% competitive, 70% leading, 10% trailing");
  assert.equal(seasonsText([2024, 2025]), "2024-2025");
  assert.equal(seasonsText([2025]), "2025");
});

test("backtest sentences say exactly what the frozen verdicts say", () => {
  const block = (v: Record<string, "improves" | "worse" | "no clear difference">, best: string) => ({
    n: 10, models: {}, comparisons: {}, verdicts: v, best_baseline: best,
  });
  const bt: Backtest = {
    status: "frozen", version: "v", spec_sha256: "x", seasons: [2019, 2020, 2021], run_at: "t",
    plays: block({ shrunk_team: "no clear difference", matchup_additive: "worse", script_mixture: "worse" }, "league_mean"),
    neutral_pass_rate: block({ shrunk_team: "improves" }, "team_to_date"),
    range_coverage: { conditional_on_script: { n: 100, inside: 82, share: 0.82, nominal: 0.8 },
      pregame_mixture: { n: 0, inside: 0, share: null, nominal: 0.8 } },
    data: { games: 10, seasons_stored: [2019] },
  };
  const s = backtestSentences(bt);
  assert.match(s.pace, /2019-2021 backtest, the shrunk plays per game did not forecast the next game better than the league mean to date/);
  assert.match(s.neutral, /better than both simple baselines/);
  assert.match(s.matchup, /worse than the league mean to date/);
  assert.equal(coverageText(bt.range_coverage.conditional_on_script), "82.0% of 100 games (nominal 80%)");
  assert.equal(coverageText(bt.range_coverage.pregame_mixture), null);
  const none = backtestSentences({ status: "not_run", message: "m" });
  assert.match(none.pace, /no claim is made/);
});

test("a result line needs both scores", () => {
  const row = { points_for: 27, points_against: 20 } as WeekRow;
  assert.equal(resultText(row), "W 27-20");
  assert.equal(resultText({ ...row, points_for: 10, points_against: 31 }), "L 10-31");
  assert.equal(resultText({ ...row, points_for: null }), null);
});
