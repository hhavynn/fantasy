import assert from "node:assert/strict";
import { test } from "node:test";
import { tradeCopyText } from "./trade-export.ts";

function example(): Parameters<typeof tradeCopyText>[0] {
  return {
    league: {season: 2026, name: "PRIVATE_LEAGUE"},
    trade: {team_a_id: 10001, team_b_id: 10002, shape: "two-for-one",
      team_a_sends: [12345, 23456], team_b_sends: [34567]},
    evaluation: {weeks:[8,9,10]},
    team_a: {team_name:"PRIVATE_TEAM_A", deltas:{ros_delta:0, unsupported_slots:[]}},
    team_b: {team_name:"PRIVATE_TEAM_B", deltas:{ros_delta:null, unsupported_slots:["IDP"]}},
    provenance: {contract:"signal-provenance.v1",signals:[{
      signal:"league",label:"PRIVATE_SOURCE_LABEL",source:"espn",captured_at:"2026-10-26T10:00:00Z",
      snapshot_version:"PRIVATE_SNAPSHOT",coverage:null,reason_unavailable:null,stale_reason:null,stale_after_hours:36,
    }]},
  } as unknown as Parameters<typeof tradeCopyText>[0];
}

test("trade export contains only authorized, aggregate values", () => {
  const t = tradeCopyText(example(), "ESPN");
  for (const secret of ["PRIVATE_LEAGUE","PRIVATE_TEAM_A","PRIVATE_TEAM_B","PRIVATE_SNAPSHOT",
    "12345","23456","34567","10001","10002","PRIVATE_SOURCE_LABEL","https://"]) {
    assert.ok(!t.includes(secret), secret);
  }
  assert.match(t,/Side A sends: 2 players/);
  assert.match(t,/Side B sends: 1 player/);
  assert.match(t,/Side A projected rest-of-season lineup change: 0\.0 points/);
  assert.match(t,/Side B projected rest-of-season lineup change: — points/);
  assert.match(t,/incomplete or unknown/);
  assert.match(t,/Unsupported lineup slots/);
  assert.match(t,/2026-10-26T10:00:00.000Z/);
});

test("signed changes are measured projections, not acceptance predictions", () => {
  const x = example();
  x.team_a.deltas.ros_delta = 4.25;
  x.team_b.deltas.ros_delta = -2.5;
  const t = tradeCopyText(x, "Fantasy Intel");
  assert.match(t,/\+4\.3 points/);
  assert.match(t,/-2\.5 points/);
  assert.match(t,/acceptance likelihood/);
  assert.doesNotMatch(t,/At least one projected total is incomplete/);
});

test("unknown dates and weeks stay unknown, not zero or current time", () => {
  const x = example();
  x.provenance = null;
  x.evaluation.weeks = [];
  const t = tradeCopyText(x, "ESPN");
  assert.match(t,/Evaluation weeks: unknown/);
  assert.match(t,/League snapshot captured: unknown/);
});
