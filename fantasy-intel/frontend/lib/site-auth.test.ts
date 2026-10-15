import assert from "node:assert/strict";
import { test } from "node:test";
import { basicAuthOk } from "./site-auth.ts";

const basic = (userPass: string) => `Basic ${btoa(userPass)}`;

test("accepts the right password with any username", () => {
  assert.equal(basicAuthOk(basic("me:hunter2"), "hunter2"), true);
  assert.equal(basicAuthOk(basic(":hunter2"), "hunter2"), true);
});

test("passwords containing a colon still match", () => {
  assert.equal(basicAuthOk(basic("me:a:b"), "a:b"), true);
});

test("rejects a wrong, missing or malformed credential", () => {
  assert.equal(basicAuthOk(basic("me:hunter3"), "hunter2"), false);
  assert.equal(basicAuthOk(basic("me:hunter22"), "hunter2"), false);
  assert.equal(basicAuthOk(basic("hunter2"), "hunter2"), false);
  assert.equal(basicAuthOk(null, "hunter2"), false);
  assert.equal(basicAuthOk("Bearer hunter2", "hunter2"), false);
  assert.equal(basicAuthOk("Basic %%%", "hunter2"), false);
});
