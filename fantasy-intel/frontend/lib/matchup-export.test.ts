import assert from "node:assert/strict";
import { test } from "node:test";
import { matchupCopyText } from "./matchup-export.ts";
import type { Matchup, TeamSide } from "./matchup-types.ts";

const side = (over: Partial<TeamSide> = {}): TeamSide => ({
  team_id: 90, name: "PRIVATE_OWNER", abbrev: "SEC", record: {wins:0,losses:0,ties:0}, standing:null,
  actual_points:null, projected_points:null, known_points:null, projection_complete:false,
  lineup:[],bench:[], actual_lineup:null, actual_bench:null, ...over,
});
const match = (over: Partial<Matchup> = {}): Matchup => ({
  league: {league_id:123456, season:2026, name:"PRIVATE_LEAGUE", current_week:7},
  week:7,phase:"regular",matchup_id:20,matchup_period:7,period_weeks:null,
  is_bye_week:false,weeks_available:[7],me:side({actual_points:105.5,projected_points:99}),
  opponent:side({name:"PRIVATE_RIVAL",actual_points:95.1}),status:"final",notes:[], ...over,
});

test("matchup copy uses confirmed actuals and never leaks identifiers or names", () => {
  const t = matchupCopyText(match(),"ESPN");
  assert.match(t,/Final/);
  assert.match(t,/105\.5 actual points/);
  assert.match(t,/95\.1 actual points/);
  for (const privateValue of ["PRIVATE_OWNER","PRIVATE_RIVAL","PRIVATE_LEAGUE","123456","team_id","http"])
    assert.ok(!t.includes(privateValue), privateValue);
});

test("in progress is marked at last sync with an authoritative snapshot timestamp", () => {
  const m=match({status:"in_progress",provenance: {
    contract:"signal-provenance.v1",signals:[{
      signal:"league",label:"Provider",source:"espn",captured_at:"2026-10-25T18:00:00Z",
      snapshot_version:"INTERNAL_PRIVATE",coverage:null,reason_unavailable:null,stale_reason:null,stale_after_hours:36,
    }],
  }});
  const t=matchupCopyText(m,"ESPN");
  assert.match(t,/In progress at last sync/);
  assert.match(t,/2026-10-25T18:00:00.000Z/);
  assert.ok(!t.includes("INTERNAL_PRIVATE"));
});

test("an upcoming match uses known projections and distinguishes zero from unknown", () => {
  const t=matchupCopyText(match({status:"upcoming",me:side({projected_points:0}),opponent:side()}),"Fantasy Intel");
  assert.match(t,/My side: 0\.0 projected points/);
  assert.match(t,/Opponent: — points unavailable/);
  assert.doesNotMatch(t,/actual points/);
});

test("bye week and partial projections avoid fabricated opponent totals", () => {
  const t=matchupCopyText(match({opponent:null,me:side({known_points:87.5})}),"ESPN");
  assert.match(t,/Opponent: No opponent scheduled/);
  assert.match(t,/87\.5 known projected points only \(incomplete\)/);
});
