import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";
import { linkGuardHash, linkGuardSecret } from "./link-guard.ts";

test("the repeat guard's key comes from a server-side secret, else the guard is off", () => {
  assert.equal(linkGuardSecret({ LINK_GUARD_SECRET: "s".repeat(32), API_KEY: "k".repeat(40) }), "s".repeat(32));
  assert.equal(linkGuardSecret({ API_KEY: "k".repeat(40) }), "k".repeat(40), "the hosted-mode API key the server already keeps");
  assert.equal(linkGuardSecret({ LINK_GUARD_SECRET: "short", API_KEY: "" }), null, "too short to be a key");
  assert.equal(linkGuardSecret({}), null, "no key: guard off, never an unkeyed hash");
});

test("the guard cookie is a keyed hash, not the guessable sha256 of the address", () => {
  const email = "member@example.test";
  const plain = createHash("sha256").update(email).digest("hex").slice(0, 32);
  const a = linkGuardHash(email, "a".repeat(32));
  assert.match(a, /^[a-f0-9]{32}$/);
  assert.notEqual(a, plain);
  assert.notEqual(a, linkGuardHash(email, "b".repeat(32)), "another deployment's key gives another value");
  assert.equal(linkGuardHash(" Member@Example.TEST ", "a".repeat(32)), a, "same normalized address, same value");
  assert.notEqual(linkGuardHash("other@example.test", "a".repeat(32)), a);
});
