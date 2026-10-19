import assert from "node:assert/strict";
import { test } from "node:test";
import { conditions, factorChips, headline, outlook, rowFlags, splitPlayers, statLineText, statusText, type KickerContext, type KickerPlayer } from "./kickers.ts";

const ctx = (over: Partial<KickerContext> = {}): KickerContext => ({
  expected_kicker_basis: "kicked the team's last game",
  designation: "none",
  venue: { venue_id: "GNB00", name: "Lambeau Field", roof: "open_air", elevation_m: 195, source: "registry", kickoff_utc: null },
  weather: { status: "unavailable", reason: "kickoff is beyond the NWS hourly forecast range", provider: "nws", retrieved_at: null,
    issued_at: null, stale: false, used_by_model: false, values: null },
  ...over,
});

const player = (id: number, status: string, exp: number | null, name = `K${id}`): KickerPlayer => ({
  espn_player_id: id, name, nfl_team: "GB", opponent: "CHI", is_home: true, fantasy_team_id: null, injury_status: null,
  independent: { projected_points: exp, expected_points: exp, p_play: 0.99, interval_80: null, coverage_status: status,
    stat_line: null, context: null, explanation: [{ feature: "headline", contribution: null, text: "Strong kicking outlook" }],
    flags: [], missing_inputs: [], pregame: true, model_version: "kfp-1.1.0" },
  espn: { projected_points: 8 }, comparison: { expected_minus_espn: null },
});

test("projected kickers sort by expected points; the rest are listed by status", () => {
  const { projected, other } = splitPlayers([player(1, "projected", 7.1), player(2, "bye", null), player(3, "projected", 9.4),
    player(4, "projected", null)]);
  assert.deepEqual(projected.map((p) => p.espn_player_id), [3, 1]);
  assert.deepEqual(other.map((p) => p.espn_player_id).sort(), [2, 4]);
});

test("a forecast that is not a model input is labeled as shown only", () => {
  const c = conditions(ctx({ weather: { status: "ok", reason: null, provider: "nws", retrieved_at: "2026-10-22T12:00:00",
    issued_at: null, stale: false, used_by_model: false,
    values: { wind_mph_mean: 17.2, gust_mph_max: 29, temp_f_mean: 41, precip_prob_max: 10, short_forecast: [] } } }));
  assert.match(c.text, /Outdoors · 17 mph wind \(gusts 29\), 41°F/);
  assert.match(c.detail ?? "", /not a model input/);
  assert.equal(c.tone, "warn");
});

test("no forecast: the venue only, with the reason", () => {
  const c = conditions(ctx());
  assert.equal(c.text, "Outdoors");
  assert.match(c.detail ?? "", /forecast range/);
  const indoor = conditions(ctx({ venue: { venue_id: "DEN00", name: "x", roof: "retractable_unknown", elevation_m: 1609, source: "registry", kickoff_utc: null } }));
  assert.equal(indoor.text, "Retractable roof (position unknown) · 1,609 m");
});

test("status, stat line and headline text", () => {
  assert.equal(statusText("not_expected_kicker"), "Not his team's kicker");
  assert.equal(statusText("something_new"), "something new");
  assert.equal(statLineText(null), null);
  assert.equal(statLineText({ fg_att: 1.96, fg_made: 1.7, pat_att: 2.4, pat_made: 2.3, p_pat: 0.95,
    fg_att_by_distance: { "0_39": 0.9, "40_49": 0.6, "50_59": 0.4, "60_plus": 0.02 },
    fg_made_by_distance: { "0_39": 0.87, "40_49": 0.5, "50_59": 0.3, "60_plus": 0.01 },
    p_make_by_distance: { "0_39": 0.96, "40_49": 0.84, "50_59": 0.72, "60_plus": 0.55 } }),
  "1.7/2.0 FG (0.9 / 0.5 / 0.3 / 0.0) · 2.3/2.4 PAT");
  assert.equal(headline(player(1, "projected", 7)), "Strong kicking outlook");
});

test("rowFlags drops the rest-of-season horizon flag and keeps the rest", () => {
  const flags = ["rest-of-season kicker forecast (horizon 2): experimental, not validated beyond the next week", "new signing"];
  assert.deepEqual(rowFlags(flags), ["new signing"]);
  assert.deepEqual(rowFlags([]), []);
});

test("factor chips are the model's point contributions, biggest first, with venue named by roof and elevation", () => {
  const p = player(1, "projected", 9.4);
  p.independent.context = ctx({ venue: { venue_id: "DEN00", name: "Empower Field", roof: "fixed", elevation_m: 1609, source: "registry", kickoff_utc: null } });
  p.independent.explanation = [
    { feature: "headline", contribution: null, text: "Strong kicking outlook" },
    { feature: "venue", contribution: 0.4, text: "venue: Empower Field" },
    { feature: "kicker_accuracy", contribution: 1.52, text: "kicker accuracy: 88% at 45 yards" },
    { feature: "long_range_share", contribution: 0.05, text: "long-range usage" },
    { feature: "pat_opportunity", contribution: -0.6, text: "touchdown volume" },
  ];
  assert.deepEqual(factorChips(p).map((c) => [c.label, c.points]),
    [["Kicker accuracy", 1.52], ["TD volume", -0.6], ["Dome / altitude", 0.4]]);
  assert.equal(factorChips(p, 1).length, 1);
});

test("the outlook drops the factor tail that the chips already show", () => {
  const p = player(1, "projected", 8.7);
  p.independent.explanation = [{ feature: "headline", contribution: null,
    text: "Strong kicking outlook: 8.7 projected vs a 7.6 median (helped by pat opportunity and venue; held back by kicker accuracy)" }];
  assert.equal(outlook(p), "Strong kicking outlook: 8.7 projected vs a 7.6 median");
  p.independent.explanation[0].text = "Average kicking outlook: 7.6 projected vs a 7.6 median";
  assert.equal(outlook(p), "Average kicking outlook: 7.6 projected vs a 7.6 median");
});
