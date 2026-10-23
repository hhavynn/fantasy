import assert from "node:assert/strict";
import test from "node:test";
import { sortRows } from "./sort.ts";
import {
  DEFAULT_SORT, analyzerHref, analyzerLabel, basisLine, changeUnavailableText, filterRows, parseValuesState,
  positionRankText, selectedRow, signedPts, valueSortValue, valuesHref, weeksText, whyText,
  type Basis, type ChangeContext, type ValueRow,
} from "./trade-values.ts";

// Invented players only ("Test Player A"...), shaped like the trade-values/v1 contract.
function row(over: Partial<ValueRow> = {}): ValueRow {
  return {
    espn_player_id: 9001, name: "Test Player A", position: "RB", pro_team: "KC", injury_status: null,
    team_id: 3, team_name: "Team 3", on_my_team: false,
    value: 24.5, value_per_week: 2.5, value_regular: 20, value_playoff: 4.5, known_value: null,
    rank: 2, rank_of: 10, position_rank: 1, position_rank_of: 4, league_starters_at_position: 8, starter_caliber: true,
    weeks_counted: 10, missing_weeks: [], nfl_ineligible_weeks: [], availability_status: "available",
    uncertainty: [], weekly: [], change: null, ...over,
  };
}

const ctx: ChangeContext = {
  available: true, reason: null, baseline: { id: "2026-10-13", label: "Oct 13 (before week 6)", taken_at: null, cycle_week: 6, model_version: null },
  weeks: [7, 8, 9, 10], excluded_weeks: [], notes: [], method: "", source: "espn",
};
const none: ChangeContext = { ...ctx, available: false, baseline: null, weeks: [], reason: "Only one ESPN projection snapshot is stored (Oct 20)." };

test("URL state: known positions and numeric ids only; owner is not the team-perspective param", () => {
  assert.deepEqual(parseValuesState({ pos: "WR", owner: "4", player: "-16012" }), { position: "WR", owner: 4, player: -16012 });
  assert.deepEqual(parseValuesState({ pos: "LB", owner: "x", player: "1e3" }), { position: null, owner: null, player: null });
  assert.deepEqual(parseValuesState({ pos: ["D/ST", "QB"], team: "2" }), { position: "D/ST", owner: null, player: null });
});

test("hrefs keep filters and sorting, and a filter change clears the selected player", () => {
  const s = { position: "RB" as const, owner: 2, player: 7 };
  assert.equal(valuesHref(s, { position: "WR" }, { sort: "change", dir: "asc" }), "/trades/values?pos=WR&owner=2&sort=change&dir=asc");
  assert.equal(valuesHref(s, { player: 9 }), "/trades/values?pos=RB&owner=2&player=9");
  assert.equal(valuesHref({ position: null, owner: null, player: null }), "/trades/values");
});

test("filters by position and owner", () => {
  const rows = [row(), row({ espn_player_id: 2, position: "WR" }), row({ espn_player_id: 3, team_id: 1 })];
  assert.deepEqual(filterRows(rows, { position: "RB", owner: 3, player: null }).map((r) => r.espn_player_id), [9001]);
  assert.equal(filterRows(rows, { position: null, owner: null, player: null }).length, 3);
});

test("a missing value or change sorts last in both directions, never as 0", () => {
  const rows = [row({ espn_player_id: 1, value: null }), row({ espn_player_id: 2, value: 0 }), row({ espn_player_id: 3, value: 5 })];
  for (const dir of ["asc", "desc"] as const) {
    const out = sortRows(rows, (r) => valueSortValue(r, DEFAULT_SORT.key), dir).map((r) => r.espn_player_id);
    assert.equal(out.at(-1), 1, dir);
  }
  const ch = [row({ espn_player_id: 1, change: null }), row({ espn_player_id: 2, change: { ...change(), delta: -1 } })];
  assert.deepEqual(sortRows(ch, (r) => valueSortValue(r, "change"), "desc").map((r) => r.espn_player_id), [2, 1]);
});

function change(): NonNullable<ValueRow["change"]> {
  return { value_before: 20, value_now: 24.5, delta: 4.5, reason: null, projection_change: 6, replacement_change: 1.5,
    position_rank_before: 2, position_rank_now: 1 };
}

test("analyzer links: ask the owner for him, or trade him away from your own team", () => {
  assert.equal(analyzerHref(row(), 2), "/trades?ta=2&tb=3&b=9001");
  assert.equal(analyzerLabel(row(), 2), "Trade for");
  assert.equal(analyzerHref(row({ team_id: 2 }), 2), "/trades?ta=2&a=9001");
  assert.equal(analyzerLabel(row({ team_id: 2 }), 2), "Trade away");
  assert.equal(analyzerHref(row(), null), "/trades?ta=3&a=9001");
});

test("why a value moved names both measured parts and the compared weeks", () => {
  assert.equal(whyText(row({ change: change() }), ctx),
    "Value up 4.5 since Oct 13 (before week 6): over weeks 7–10, his own projection rose 6.0 and the RB replacement level rose 1.5.");
  assert.equal(whyText(row({ change: { ...change(), delta: 0, projection_change: 0, replacement_change: -0.2 } }), ctx),
    "Value unchanged since Oct 13 (before week 6): over weeks 7–10, his own projection did not change and the RB replacement level fell 0.2.");
  assert.match(whyText(row({ change: { ...change(), projection_change: null } }), ctx)!, /own projection change is unknown/);
  assert.equal(whyText(row({ change: { ...change(), delta: null, reason: "No projection for week 8 in the earlier snapshot." } }), ctx),
    "No projection for week 8 in the earlier snapshot.");
});

test("without a prior snapshot there is no why, and the reason is the backend's", () => {
  assert.equal(whyText(row({ change: change() }), none), null);
  assert.equal(whyText(row(), ctx), null);
  assert.equal(changeUnavailableText(none), none.reason);
  assert.match(changeUnavailableText(null), /No earlier projection snapshot/);
});

test("wording helpers", () => {
  assert.equal(signedPts(1.26), "+1.3");
  assert.equal(signedPts(-0.04), "0.0");
  assert.equal(signedPts(null), null);
  assert.equal(weeksText([3, 4, 5, 9]), "weeks 3–5, 9");
  assert.equal(weeksText([4]), "week 4");
  assert.equal(positionRankText(row()), "RB1 of 4");
  assert.equal(positionRankText(row({ position_rank: null })), null);
  assert.equal(selectedRow([row()], 9001)?.name, "Test Player A");
  assert.equal(selectedRow([row()], 5), null);
  const b = { label: "Fantasy Intel model estimate", projection_label: "ESPN weekly projections", calculation_version: "tv-1",
    window: { weeks: [3, 4, 5] } } as unknown as Basis;
  assert.equal(basisLine(b), "Fantasy Intel model estimate · ESPN weekly projections · weeks 3–5 · calculation tv-1");
});
