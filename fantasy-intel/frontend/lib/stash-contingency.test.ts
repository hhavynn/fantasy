import assert from "node:assert/strict";
import { test } from "node:test";
import { contingencyStatus } from "./stash-contingency.ts";

test("unavailable contingency evidence is an abstention, not a zero-scenario prediction", () => {
  assert.equal(contingencyStatus({available:false,items:[],reason:"No synced role changes"}),
    "Conditional opportunities cannot be evaluated: No synced role changes");
  assert.match(contingencyStatus({available:false,items:[]})!, /cannot be evaluated/);
  assert.match(contingencyStatus(undefined)!, /not returned/);
});

test("empty observed opportunity tree is explicit, not conflated with unavailable evidence", () => {
  assert.match(contingencyStatus({available:true,items:[],notices:[{code:"no_depth_charts",text:"No depth charts synced"}]})!,
    /No evidence-backed conditional opportunities/);
  assert.equal(contingencyStatus({available:true,items:[{starter:"example"}]}),null);
});
