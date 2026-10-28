import assert from "node:assert/strict";
import { test } from "node:test";
import { rosterCopyText } from "./roster-export.ts";

function scenario(): Parameters<typeof rosterCopyText>[0] {
  return {
    league: {name:"PRIVATE_LEAGUE",season:2026,current_week:8,playoff_teams:4,last_regular_season_week:14},
    window: {weeks:[8,9,10],first_actionable_week:8,last_regular_season_week:14,final_week:17,reason:null,uncertain:false,warning:null},
    freshness: {league_synced_at:"2026-10-27T12:00:00Z",projections_synced_at:null},
    players: [
      {espn_player_id:123456,name:"PRIVATE_OWNER_PLAYER",on_ir:false,on_taxi:false},
      {espn_player_id:234567,name:"PRIVATE_IR_PLAYER",on_ir:true,on_taxi:false},
      {espn_player_id:345678,name:"PRIVATE_TAXI_PLAYER",on_ir:false,on_taxi:true},
    ],
    overview: {phases: {
      all:{weeks:[8,9],projected_points:null,known_points:0,incomplete_weeks:[9]},
      regular:{weeks:[8,9],projected_points:null,known_points:0,incomplete_weeks:[9]},
      playoff:{weeks:[15],projected_points:0,known_points:0,incomplete_weeks:[]},
    }},
  } as unknown as Parameters<typeof rosterCopyText>[0];
}
test("roster summary excludes names, ids and league details while preserving missing/zero semantics", () => {
  const d=scenario();
  const t=rosterCopyText(d,"all");
  assert.match(t,/Rostered players: 3/);
  assert.match(t,/Active roster players: 1/);
  assert.match(t,/IR: 1 · Taxi: 1/);
  assert.match(t,/Projected starting-lineup points: —/);
  assert.match(t,/Known-only starting-lineup points: 0\.0 \(partial or unavailable\)/);
  assert.match(t,/Incomplete projection weeks: 9/);
  assert.match(t,/2026-10-27T12:00:00.000Z/);
  assert.match(t,/Projection snapshot captured: unknown/);
  for (const secret of ["PRIVATE_", "123456", "234567", "345678", "owner_id", "league_id", "http"]) {
    assert.ok(!t.includes(secret), secret);
  }
});
test("measured zero is not treated as missing in a complete playoff window", () => {
  const t=rosterCopyText(scenario(),"playoff");
  assert.match(t,/Projected starting-lineup points: 0\.0/);
  assert.match(t,/Known-only starting-lineup points: 0\.0 \(complete\)/);
});
test("an empty roster response is explicitly unavailable, never zero", () => {
  const d=scenario();
  d.players=undefined;
  d.overview=undefined;
  d.freshness.league_synced_at=null;
  const t=rosterCopyText(d,"regular");
  assert.match(t,/Rostered players: unknown/);
  assert.match(t,/Active roster players: unknown/);
  assert.match(t,/Incomplete projection weeks: unknown/);
  assert.match(t,/League snapshot captured: unknown/);
});
