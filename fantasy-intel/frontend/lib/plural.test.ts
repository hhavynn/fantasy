import assert from "node:assert/strict";
import { test } from "node:test";
import { plural } from "./plural.ts";

test("plural: exactly one is singular; zero and many are plural", () => {
  assert.equal(plural(1, "game"), "game");
  assert.equal(plural(0, "game"), "games");
  assert.equal(plural(2, "game"), "games");
});

test("plural: an irregular plural can be given", () => {
  assert.equal(plural(1, "day", "days"), "day");
  assert.equal(plural(3, "category", "categories"), "categories");
});
