import assert from "node:assert/strict";
import test from "node:test";
import { shortlistKey, parseShortlist, toggleShortlist, readShortlist, writeShortlist } from "./waiver-shortlist.ts";

const A = "lg_aaaaaaaaaaaaaaaa";
const B = "lg_bbbbbbbbbbbbbbbb";

test("canonical league IDs get distinct keys; unscoped and malformed IDs cannot persist", () => {
  assert.equal(shortlistKey(A), "fi:waiver-shortlist:v1:lg_aaaaaaaaaaaaaaaa");
  assert.notEqual(shortlistKey(A), shortlistKey(B));
  for (const id of [null, "", "12345", "lg_bad", A + "/waivers"]) assert.equal(shortlistKey(id), null);
});

test("save and remove preserve unique stable IDs without changing the input", () => {
  const ids = [101];
  assert.deepEqual(toggleShortlist(ids, 202), [101, 202]);
  assert.deepEqual(toggleShortlist(ids, 101), []);
  assert.deepEqual(ids, [101]);
  assert.deepEqual(toggleShortlist(ids, NaN), [101]);
});

test("missing storage starts empty and valid stored IDs are deduplicated", () => {
  assert.deepEqual(parseShortlist(null), []);
  assert.deepEqual(parseShortlist("[101,202,101]"), [101, 202]);
});

test("malformed, legacy objects, and invalid player IDs are rejected", () => {
  for (const raw of ["{", "null", "{}", '"101"', '[{"espn_player_id":101}]', "[0]", "[1.5]", '["101"]', "[9007199254740992]", "[101,null]"])
    assert.deepEqual(parseShortlist(raw), [], raw);
});

test("reload restores IDs only and league writes never overwrite another league", () => {
  const storage = new Map<string, string>();
  const io = { getItem: (k: string) => storage.get(k) ?? null, setItem: (k: string, v: string) => { storage.set(k, v); } };
  assert.equal(writeShortlist(io, A, [101]), true);
  assert.equal(writeShortlist(io, B, [202]), true);
  assert.deepEqual(readShortlist(io, A), { ids: [101], available: true });
  assert.deepEqual(readShortlist(io, B), { ids: [202], available: true });
  assert.equal(storage.get("fi:waiver-shortlist:v1:lg_aaaaaaaaaaaaaaaa"), "[101]");
  assert.equal(writeShortlist(io, null, [303]), false);
  assert.equal(storage.size, 2);
});

test("unavailable storage fails safely; an unscoped read never touches storage", () => {
  let reads = 0;
  const io = { getItem: () => { reads++; throw new Error("denied"); }, setItem: () => { throw new Error("quota"); } };
  assert.deepEqual(readShortlist(io, A), { ids: [], available: false });
  assert.equal(writeShortlist(io, A, [101]), false);
  assert.deepEqual(readShortlist(io, null), { ids: [], available: false });
  assert.equal(reads, 1, "unscoped reads must not access storage");
});

test("defense IDs survive save, persistence, reload, and remove alongside offensive players", () => {
  const ids = toggleShortlist([101], -16012);
  assert.deepEqual(ids, [101, -16012]);
  assert.deepEqual(parseShortlist(JSON.stringify(ids)), [101, -16012]);
  assert.deepEqual(toggleShortlist(ids, -16012), [101]);
});
