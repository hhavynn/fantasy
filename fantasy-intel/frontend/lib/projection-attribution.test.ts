import assert from "node:assert/strict";
import test from "node:test";
import {
  attributionHeadline, attributionRows, inputValue, kindText, reconcileText, sideText, signedText, unattributedInputs, weeksText,
  type Attribution,
} from "./projection-attribution.ts";
import { moversApiQuery, parseMoversState } from "./projection-movers.ts";

// Synthetic block following projection-attribution/v1 (invented numbers).
const base: Attribution = {
  contract: "projection-attribution/v1", status: "explained", source_label: "Fantasy Intel model", method: "exact_linear",
  method_note: "Both forecasts come from the same model artifact.", approximation_note: null, total: 1.8,
  parts: [
    { key: "input:targets_pg", kind: "input", label: "targets per game", points: 0.9, weeks: [7, 8], exact: true, feature: "targets_pg",
      unit: "number", before: 5, after: 6, before_missing: null, after_missing: null, varies: false },
    { key: "input:snap_pct", kind: "input", label: "offensive snap share", points: 0.9, weeks: [7], exact: true, feature: "snap_pct",
      unit: "share", before: null, after: 0.7, before_missing: "unknown when the forecast was made", after_missing: null, varies: false },
    { key: "availability", kind: "availability", label: "Chance he plays", points: 0, weeks: [7, 8], exact: true, unit: "share",
      before: 0.9, after: 0.9, before_missing: null, after_missing: null, varies: false },
  ],
  residual: { points: 0, reasons: [] }, reconciles: true, tolerance: 0.03,
  inputs: [
    { feature: "targets_pg", label: "targets per game", unit: "number", weeks: [7, 8], before: 5, after: 6, before_missing: null,
      after_missing: null, varies: false },
    { feature: "pts_l3", label: "points per game, last 3", unit: "number", weeks: [7], before: 8, after: null, before_missing: null,
      after_missing: "not among the inputs this forecast stored", varies: false },
  ],
  missing_inputs: { before: [], after: [] }, model: { changed: false, reasons: [] },
  as_of: {
    before: { created_at: "2026-10-14T12:00:00Z", data_through: { season: 2026, week: 5 }, cutoff_week: 6, model_versions: ["ifp-2.2.0"],
      artifact_ids: ["a"], run_ids: ["r1"] },
    after: { created_at: "2026-10-21T12:00:00Z", data_through: null, cutoff_week: 7, model_versions: ["ifp-2.2.0"], artifact_ids: ["a"],
      run_ids: ["r2"] },
    refused: [],
  },
  reason: null,
};

test("weeksText collapses runs and names one week in the singular", () => {
  assert.equal(weeksText([9, 7, 8, 11]), "weeks 7–9, 11");
  assert.equal(weeksText([7]), "week 7");
  assert.equal(weeksText([]), "");
});

test("inputValue formats by unit and never turns a missing value into 0", () => {
  assert.equal(inputValue(0.62, "share"), "62%");
  assert.equal(inputValue(1, "flag"), "yes");
  assert.equal(inputValue(0, "flag"), "no");
  assert.equal(inputValue(0.0345, "number"), "0.035");
  assert.equal(inputValue(6.04, "number"), "6");
  assert.equal(inputValue(0, "number"), "0");
  assert.equal(inputValue(null, "number"), null);
  assert.equal(inputValue(undefined, "share"), null);
});

test("signedText uses a real minus sign and shows a measured zero as 0.00", () => {
  assert.equal(signedText(1.234), "+1.23");
  assert.equal(signedText(-0.4), "−0.40");
  assert.equal(signedText(-0.001), "0.00");
});

test("the headline says how much is explained, in words, for every status", () => {
  assert.match(attributionHeadline(base), /whole change/);
  assert.match(attributionHeadline({ ...base, status: "partial", residual: { points: -0.8, reasons: [] } }), /−0\.80 points/);
  assert.match(attributionHeadline({ ...base, status: "model_changed" }), /model changed/);
  assert.match(attributionHeadline({ ...base, status: "not_decomposed" }), /No part/);
  const espn = { ...base, status: "unavailable" as const, source_label: "ESPN", reason: "ESPN publishes one projected total per week." };
  assert.equal(attributionHeadline(espn), espn.reason);
});

test("reconcileText checks the parts and residual against the total", () => {
  assert.match(reconcileText(base) ?? "", /^Parts and residual add up to the change of \+1\.80 points\. .*±0\.03/);
  const off = { ...base, reconciles: false, total: 2 };
  assert.match(reconcileText(off) ?? "", /add up to \+1\.80 against a change of \+2\.00/);
  assert.equal(reconcileText({ ...base, total: null }), null);
});

test("rows keep measured zeros and inputs without a share are listed separately", () => {
  assert.deepEqual(attributionRows(base).map((p) => p.key), ["input:targets_pg", "input:snap_pct", "availability"]);
  assert.deepEqual(unattributedInputs(base).map((i) => i.feature), ["pts_l3"]);
});

test("kind and as-of wording are words, not colours", () => {
  assert.equal(kindText("input"), "Measured input");
  assert.equal(kindText("input", false), "Input (approximate)");
  assert.equal(kindText("model"), "Model changed");
  assert.equal(sideText(base.as_of?.before), "made Oct 14 12:00 UTC, games through 2026 week 5");
  assert.equal(sideText(base.as_of?.after), "made Oct 21 12:00 UTC, data cutoff not recorded");
  assert.equal(sideText(null), "not stored");
});

test("the API query asks for the selected player's split only for Fantasy Intel's forecasts", () => {
  const ours = parseMoversState({ source: "independent", player: "7" });
  assert.equal(moversApiQuery(ours), "source=independent&view=ros&limit=15&player=7");
  assert.equal(moversApiQuery(parseMoversState({ player: "7" })), "source=espn&view=ros&limit=15");
  assert.equal(moversApiQuery(parseMoversState({ source: "independent" })), "source=independent&view=ros&limit=15");
});
