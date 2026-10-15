import test from "node:test";
import assert from "node:assert/strict";
import type { TvPlayer } from "./types.ts";
import {
  knownMarginalFor, marginalFor, marginalSortValue, parsePhase, phaseState, recordText, topContributors, withParams,
} from "./team-value-helpers.ts";

function player(id: number, name: string, marginal: Partial<TvPlayer["marginal"]>): TvPlayer {
  return {
    espn_player_id: id, name, position: "RB", pro_team: "XX", injury_status: null, on_ir: false,
    espn_projected: {} as TvPlayer["espn_projected"],
    marginal: { all: null, regular: null, playoff: null, known_all: null, known_regular: null, known_playoff: null, ...marginal },
    weeks_started: [], weeks_benched: [], weeks_expected: 0, projection_complete: true, weekly: [], bench: [],
    bench_reason_counts: {}, unused_projected_points: 0, started_points: 0, ir_activation_sensitivity: null,
  };
}

test("parsePhase falls back to the full window", () => {
  assert.equal(parsePhase("regular"), "regular");
  assert.equal(parsePhase("playoff"), "playoff");
  assert.equal(parsePhase("all"), "all");
  assert.equal(parsePhase("bogus"), "all");
  assert.equal(parsePhase(undefined), "all");
});

test("marginal accessors pick the right window and never turn missing into zero", () => {
  const p = player(1, "A", { all: 10, regular: 7, playoff: 3, known_all: 10, known_regular: 7, known_playoff: 3 });
  assert.equal(marginalFor(p, "all"), 10);
  assert.equal(marginalFor(p, "regular"), 7);
  assert.equal(marginalFor(p, "playoff"), 3);
  const partial = player(2, "B", { all: null, known_all: 4.5 });
  assert.equal(marginalFor(partial, "all"), null);
  assert.equal(knownMarginalFor(partial, "all"), 4.5);
  assert.equal(marginalSortValue(partial, "all"), 4.5);
  assert.equal(marginalSortValue(player(3, "C", {}), "all"), null);
});

test("topContributors orders by lineup marginal, skips zero and flags partial values", () => {
  const rows = [
    player(1, "Low", { all: 2, known_all: 2 }),
    player(2, "High", { all: 30, known_all: 30 }),
    player(3, "Zero", { all: 0, known_all: 0 }),
    player(4, "Partial", { all: null, known_all: 12 }),
    player(5, "Missing", {}),
  ];
  const top = topContributors(rows, "all", 3);
  assert.deepEqual(top.map((t) => t.name), ["High", "Partial", "Low"]);
  assert.deepEqual(top.map((t) => t.exact), [true, false, true]);
});

test("recordText formats wins, losses and optional ties", () => {
  assert.equal(recordText({ wins: 1, losses: 4, ties: 0 }), "1-4");
  assert.equal(recordText({ wins: 6, losses: 6, ties: 1 }), "6-6-1");
  assert.equal(recordText(null), "—");
});

test("withParams keeps unrelated params and drops removed ones", () => {
  const sp = { team: "6", phase: "playoff", sort: "regular", dir: "desc" };
  assert.equal(withParams("/my-team/value", sp, { phase: "regular" }), "/my-team/value?team=6&sort=regular&dir=desc&phase=regular");
  assert.equal(withParams("/my-team/value", sp, { phase: null, sort: null, dir: null }), "/my-team/value?team=6");
  assert.equal(withParams("/my-team/value", {}, { phase: null }), "/my-team/value");
  assert.equal(withParams("/my-team/value", { trades: "off" }, { trades: "on" }), "/my-team/value?trades=on");
});

test("phaseState separates a phase with no weeks left from one with incomplete projections", () => {
  assert.equal(phaseState({ weeks: [], projected_points: null }), "empty");
  assert.equal(phaseState({ weeks: [9, 10], projected_points: null }), "incomplete");
  assert.equal(phaseState({ weeks: [9, 10], projected_points: 210.5 }), "complete");
  assert.equal(phaseState({ weeks: [], projected_points: 0 }), "empty");
});
