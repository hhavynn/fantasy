import assert from "node:assert/strict";
import { test } from "node:test";
import { effectivePool, parseAssets, parsePool, pathText, questionText, utilityQuery } from "./roster-utility.ts";

const R = { names: { "1": { name: "Ann QB", position: "QB", pro_team: "KC" }, "7": { name: "Bo WR", position: "WR", pro_team: "BUF" } } };

test("assets are roster players, once each, at most three", () => {
  assert.deepEqual(parseAssets([7, 99, 1, 7, 3, 4], [1, 3, 4, 7]), [7, 1, 3]);
  assert.deepEqual(parseAssets([], [1]), []);
});

test("pools and the backend query", () => {
  assert.equal(parsePool("waiver"), "waiver");
  assert.equal(parsePool("trade"), "bench");
  assert.equal(parsePool(undefined), "bench");
  assert.equal(effectivePool([1, 7], "waiver"), "bench");
  assert.equal(effectivePool([1], "waiver"), "waiver");
  assert.equal(utilityQuery([7, 1], "bench", undefined), "ids=7&ids=1");
  assert.equal(utilityQuery([7], "waiver", "3"), "ids=7&pool=waiver&team_id=3");
});

test("the replacement path and the question read as plain words", () => {
  assert.equal(pathText(R, { left_lineup: [1], entered_lineup: [7] }), "Ann QB out; Bo WR in");
  assert.equal(pathText(R, { left_lineup: [1], entered_lineup: [] }), "Ann QB out; nobody fills in");
  assert.equal(pathText(R, { left_lineup: [], entered_lineup: [] }), "No lineup change");
  assert.equal(pathText(R, { left_lineup: [55], entered_lineup: [] }), "Player 55 out; nobody fills in");
  assert.equal(questionText({ ...R, asset_ids: [1, 7], pool: "bench" }), "Without Ann QB and Bo WR, from your bench");
  assert.equal(questionText({ ...R, asset_ids: [7], pool: "waiver" }), "Without Bo WR, adding the best available free agent");
});
