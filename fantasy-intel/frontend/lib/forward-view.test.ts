import assert from "node:assert/strict";
import test from "node:test";
import { modelBadge, oursBlock, oursCell, oursNotice, oursPoints, oursTotalNote } from "./forward-view.ts";
import { SWITCH_SCOPE, withSource } from "./projection-source.ts";
import type { ForwardProjection, ForwardWeek, OursForward, OursWeek } from "./types.ts";

const week = (n: number, phase: "regular" | "playoff" = "regular", on_bye = false): ForwardWeek => ({
  week: n, phase, opponent: "BUF", is_home: true, on_bye, espn_projected_points: 10 + n, projection_available: true,
});
const ow = (n: number, origin: OursWeek["origin"], value: number | null, over: Partial<OursWeek> = {}): OursWeek => ({
  week: n, value, origin, on_bye: false, run_id: "r", model_version: "ifp-2.0.0", run_created_at: "2026-10-14 12:00:00",
  horizon: n - 6, forecast_horizon: n - 6, projected_from_week: 6, horizon_note: n > 6 ? `rest-of-season forecast (horizon ${n - 6})` : null, experimental: n > 6, ...over,
});
const ours = (weeks: OursWeek[], over: Partial<OursForward> = {}): OursForward => ({
  available: true, reason: null, label: "Fantasy Intel projections (ifp-2.0.0); experimental beyond next week",
  model_version: "ifp-2.0.0", model_versions: ["ifp-2.0.0"], mixed_models: false, weeks, ros_projected_points: 30, ros_weeks_expected: weeks.length,
  ros_weeks_available: weeks.length, ros_complete: true, fallback_weeks: [], missing_weeks: [], other_model_weeks: [],
  last_regular_season_week: 14, run_created_at: "2026-10-14 12:00:00", ...over,
});

test("oursPoints charts only our numbers: ESPN fallback weeks, byes and the playoffs are gaps", () => {
  const weeks = [week(6), week(7), week(8), week(9, "regular", true), week(15, "playoff")];
  const o = ours([ow(6, "ours", 14.5), ow(7, "espn", 17), ow(8, "ours", 16.2), ow(9, "ours_zero", 0, { on_bye: true })]);
  assert.deepEqual(oursPoints(weeks, o), [14.5, null, 16.2, null, null]);     // our 0 for a bye is a gap, not a plotted zero
  const zero = ours([ow(6, "ours", 14.5), ow(7, "ours_zero", 0)]);              // unavailable (not a bye): a real 0
  assert.deepEqual(oursPoints([week(6), week(7)], zero), [14.5, 0]);
});

test("oursCell marks ESPN-fallback weeks and keeps the horizon flag without a caveat in the title", () => {
  assert.deepEqual(oursCell(ow(6, "ours", 14.5)), { value: 14.5, kind: "ours", experimental: false, model: null, title: "Fantasy Intel projection" });
  const later = oursCell(ow(8, "ours", 16.2));
  assert.equal(later.kind, "ours");
  assert.equal(later.experimental, true);
  assert.equal(later.title, "Fantasy Intel projection");
  const fb = oursCell(ow(9, "espn", 11));
  assert.equal(fb.kind, "espn");
  assert.equal(fb.value, 11);
  assert.match(fb.title, /ESPN/);
  assert.equal(oursCell(ow(10, "ours_zero", 0)).kind, "zero");
  assert.equal(oursCell(ow(11, "bye", null)).kind, "bye");
  assert.equal(oursCell(undefined).kind, "none");
});

test("a week shown from the previous model carries a model badge and the reason is shown with ours available", () => {
  const prev = ow(6, "ours", 14.5, { model_version: "ifp-1.0.0" });
  assert.equal(oursCell(prev, "ifp-2.0.0").model, "ifp-1");
  assert.equal(oursCell(ow(7, "ours", 15), "ifp-2.0.0").model, null);
  assert.equal(modelBadge("ifp-1.0.0", "ifp-1.0.0"), null);
  const o = ours([prev, ow(7, "ours", 15)], {
    reason: "No ifp-2.0.0 run is stored for week 6 yet, so the previous model ifp-1.0.0 is shown. Mixed models: total hidden.",
    mixed_models: true, model_versions: ["ifp-1.0.0", "ifp-2.0.0"], other_model_weeks: [6],
    ros_complete: false, ros_projected_points: null, missing_weeks: [6],
  });
  const n = oursNotice(o);
  assert.equal(n?.title, "Mixed projection models");
  assert.match(n?.text ?? "", /previous model ifp-1\.0\.0/);
  assert.equal(oursNotice(ours([ow(6, "ours", 1)])), null);
  assert.equal(oursNotice(ours([], { available: false, reason: "No run yet" }))?.text, "No run yet");
});

test("oursTotalNote explains an incomplete total", () => {
  const ok = ours([ow(6, "ours", 1)]);
  assert.equal(oursTotalNote(ok), "1/1 wk");
  const bad = ours([ow(6, "ours", 1), ow(7, "espn", 2)], { ros_complete: false, ros_projected_points: null, ros_weeks_available: 1, ros_weeks_expected: 2, missing_weeks: [7], fallback_weeks: [7] });
  assert.equal(oursTotalNote(bad), "1/2 wk · ESPN's number in wk 7");
});

test("oursBlock is absent for the ESPN view and the visitor asks for the forward view with source=independent", () => {
  const f = { source: "ESPN", note: "", summary: {}, projection_synced_at: null, weeks: [], schedule_available: true } as unknown as ForwardProjection;
  assert.equal(oursBlock(f), null);
  assert.equal(oursBlock({ ...f, ours: ours([]) })?.model_version, "ifp-2.0.0");
  assert.equal(withSource("/api/players/7/forward", "ours"), "/api/players/7/forward?source=independent");
  assert.equal(withSource("/api/players/7/forward", "espn"), "/api/players/7/forward");
});

test("the switch scope includes the player forward view and keeps the ESPN-only features", () => {
  assert.match(SWITCH_SCOPE, /forward view/);
  assert.match(SWITCH_SCOPE, /Trades, waivers, team value and the playoff race stay on ESPN/);
});

test("oursNotice picks its title from the fields: ESPN fallback, previous model, mixed, or another reason", () => {
  const w = [ow(6, "ours", 1)];
  const unverified = "The approval of this run's artifact could not be verified (no approved_artifacts table yet).";
  assert.equal(oursNotice(ours(w, { reason: unverified }))?.title, "Ours: see note");        // every week is ours
  assert.equal(oursNotice(ours(w, { reason: "x", fallback_weeks: [7] }))?.title, "Ours: some weeks use ESPN");
  assert.equal(oursNotice(ours(w, { reason: "x", other_model_weeks: [6] }))?.title, "Previous model in some weeks");
  assert.equal(oursNotice(ours(w, { reason: "x", other_model_weeks: [6], mixed_models: true }))?.title, "Mixed projection models");
  assert.equal(oursNotice(ours(w, { reason: unverified }))?.text, unverified);
});

test("oursTotalNote names previous-model weeks apart from ESPN fallback weeks", () => {
  const base = { ros_complete: false, ros_projected_points: null, ros_weeks_available: 1, ros_weeks_expected: 4 };
  const w = [ow(6, "ours", 1)];
  assert.equal(oursTotalNote(ours(w, { ...base, missing_weeks: [7], other_model_weeks: [7] })), "1/4 wk · previous model in wk 7");
  assert.equal(oursTotalNote(ours(w, { ...base, missing_weeks: [7, 8, 9], other_model_weeks: [7], fallback_weeks: [8, 9] })),
    "1/4 wk · previous model in wk 7; ESPN's number in wk 8, 9");
  assert.equal(oursTotalNote(ours(w, { ...base, missing_weeks: [] })), "1/4 wk · incomplete");
});
