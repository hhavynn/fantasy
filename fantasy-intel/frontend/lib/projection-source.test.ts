import assert from "node:assert/strict";
import test from "node:test";
import { ESPN_PROFILE, SLEEPER_PROFILE } from "./league-profile.ts";
import {
  effectiveProjectionSource, horizonCaution, modelStatusLine, parseProjectionSource, projectionSwitchVisible, showingOurs, withSource,
  noProjection,
} from "./projection-source.ts";

test("effectiveProjectionSource: the cookie decides for ESPN leagues, Sleeper is always Ours", () => {
  assert.equal(effectiveProjectionSource("espn", ESPN_PROFILE), "espn");
  assert.equal(effectiveProjectionSource("ours", ESPN_PROFILE), "ours");
  assert.equal(effectiveProjectionSource("espn", SLEEPER_PROFILE), "ours");
  assert.equal(effectiveProjectionSource("ours", SLEEPER_PROFILE), "ours");
  assert.equal(effectiveProjectionSource("espn", null), "espn");
  assert.equal(withSource("/api/matchup", effectiveProjectionSource("espn", SLEEPER_PROFILE)), "/api/matchup?source=independent");
});

test("the ESPN / Ours switch shows only where the provider has projections", () => {
  assert.equal(projectionSwitchVisible(ESPN_PROFILE), true);
  assert.equal(projectionSwitchVisible(SLEEPER_PROFILE), false);
  assert.equal(projectionSwitchVisible(undefined), true);
});

test("parseProjectionSource defaults to ESPN for anything but 'ours'", () => {
  assert.equal(parseProjectionSource("ours"), "ours");
  assert.equal(parseProjectionSource("espn"), "espn");
  assert.equal(parseProjectionSource(undefined), "espn");
  assert.equal(parseProjectionSource("independent"), "espn");
});

test("withSource adds source=independent only for ours", () => {
  assert.equal(withSource("/api/matchup", "espn"), "/api/matchup");
  assert.equal(withSource("/api/matchup", "ours"), "/api/matchup?source=independent");
  assert.equal(withSource("/api/start-sit?week=6", "ours"), "/api/start-sit?week=6&source=independent");
  assert.equal(withSource("/api/start-sit?", "ours"), "/api/start-sit?source=independent");
});

test("showingOurs needs an available independent run", () => {
  const base = { reason: null, run: null, model_status: null, counts: null };
  assert.equal(showingOurs({ ...base, source: "independent", available: true }), true);
  assert.equal(showingOurs({ ...base, source: "independent", available: false }), false);
  assert.equal(showingOurs({ ...base, source: "espn", available: true }), false);
  assert.equal(showingOurs(undefined), false);
});

test("modelStatusLine names the model behind the numbers, plainly", () => {
  const governance = {
    production_version: "ifp-2.0.0", fallback_version: "ifp-1.0.0", served_version: "ifp-2.0.0", is_fallback: false,
    production_approval: "owner_override", espn_superiority_established: false, start_sit_ready: false,
    production_evidence: "Historical research only", status_note: null,
  };
  const prod = { source: "independent" as const, available: true, reason: null, counts: null, governance,
    run: { run_id: "r", model_version: "ifp-2.0.0", created_at: "t" },
    model_status: { QB: "owner_override", RB: "owner_override", WR: "owner_override", TE: "owner_override" } };
  const line = modelStatusLine(prod);
  assert.equal(line, "Expected points from ifp-2.0.0, Fantasy Intel's own projection model.");
  assert.doesNotMatch(line, /owner override|experimental|validated|better than ESPN/i);
  const fallback = { ...prod, run: { ...prod.run, model_version: "ifp-1.0.0" },
    governance: { ...governance, served_version: "ifp-1.0.0", is_fallback: true },
    model_status: { QB: "validated", RB: "experimental", WR: "experimental", TE: "experimental" } };
  assert.equal(modelStatusLine(fallback), "Fallback model ifp-1.0.0 (no eligible ifp-2.0.0 run this week).");
});

test("horizonCaution notes a rest-of-season run only when it is shown as ours", () => {
  const base = { source: "independent" as const, available: true, reason: null, counts: null, model_status: null };
  const next = { ...base, run: { run_id: "r", model_version: "ifp-2.0.0", created_at: "t", forecast_horizon: 0, horizon_note: null } };
  assert.equal(horizonCaution(next), null);
  assert.equal(horizonCaution({ ...base, run: { run_id: "r", model_version: "ifp-2.0.0", created_at: "t" } }), null);
  const later = { ...base, run: { ...next.run, forecast_horizon: 3, horizon_note: "rest-of-season forecast (horizon 3): experimental, not validated beyond the next week" } };
  assert.equal(horizonCaution(later), "This is a rest-of-season forecast made 3 weeks ahead; it refreshes once a newer run is in.");
  assert.match(horizonCaution({ ...later, run: { ...later.run, forecast_horizon: 1, projected_from_week: 5 } }) ?? "", /made 1 week ahead \(from week 5\)/);
  assert.doesNotMatch(horizonCaution(later) ?? "", /experimental|validated/);
  assert.equal(horizonCaution({ ...later, source: "espn" }), null);
  assert.equal(horizonCaution({ ...later, available: false }), null);
  assert.equal(horizonCaution(null), null);
});

test("noProjection names players with no number, never as ESPN's (#254)", () => {
  assert.equal(noProjection({ unavailable: 2 }), ", 2 with no projection");
  assert.equal(noProjection({}), "");
  assert.equal(noProjection({ unavailable: 0 }), "");
});
