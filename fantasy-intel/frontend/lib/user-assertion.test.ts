import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { test } from "node:test";
import {
  ASSERTION_AUDIENCE, MAX_TTL_SECONDS, b64url, devUserId, encodeUserAssertion, isValidUserId, usableSecret,
} from "./user-assertion.ts";

const SECRET = "0123456789abcdef0123456789abcdef";
// Computed independently with Python's hmac/hashlib/base64 (see the brief's format).
const VECTOR = {
  sub: "user-123",
  iat: 1700000000,
  payloadJson: '{"sub":"user-123","aud":"fantasy-intel-api","iat":1700000000,"exp":1700000300}',
  payload: "eyJzdWIiOiJ1c2VyLTEyMyIsImF1ZCI6ImZhbnRhc3ktaW50ZWwtYXBpIiwiaWF0IjoxNzAwMDAwMDAwLCJleHAiOjE3MDAwMDAzMDB9",
  signature: "x6nCa_sr49CF4HMqvuIc_-F5AarqAyimKN5WwkP-Gz0",
};

test("known vector: v1.<payload>.<signature> matches the independently computed value", () => {
  const value = encodeUserAssertion({ sub: VECTOR.sub, secret: SECRET, now: VECTOR.iat, ttlSeconds: 300 });
  assert.equal(value, `v1.${VECTOR.payload}.${VECTOR.signature}`);
});

test("payload is the documented JSON with aud, iat and exp", () => {
  const value = encodeUserAssertion({ sub: VECTOR.sub, secret: SECRET, now: VECTOR.iat });
  const payload = value.split(".")[1];
  assert.equal(Buffer.from(payload, "base64url").toString("utf8"), VECTOR.payloadJson);
  const parsed = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
  assert.equal(parsed.aud, ASSERTION_AUDIENCE);
  assert.ok(parsed.exp - parsed.iat <= MAX_TTL_SECONDS);
});

test("the signature verifies by recomputing HMAC-SHA256 over 'v1.' + payload with standard base64 converted by hand", () => {
  for (const sub of ["a", "ab", "abc", "abcd", "user@example.com", "google|1234567890", "u".repeat(128)]) {
    const value = encodeUserAssertion({ sub, secret: SECRET, now: 1_800_000_000 });
    const [version, payload, signature, ...rest] = value.split(".");
    assert.equal(rest.length, 0);
    assert.equal(version, "v1");
    const standard = createHmac("sha256", Buffer.from(SECRET, "utf8")).update(`v1.${payload}`, "utf8").digest("base64");
    const expected = standard.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    assert.equal(signature, expected);
    // A different secret or a tampered payload must not verify.
    const other = createHmac("sha256", SECRET + "x").update(`v1.${payload}`).digest("base64url");
    assert.notEqual(signature, other);
    const tampered = createHmac("sha256", SECRET).update(`v1.${payload}A`).digest("base64url");
    assert.notEqual(signature, tampered);
  }
});

test("base64url never contains '=', '+' or '/' whatever the payload length", () => {
  for (let n = 1; n <= 40; n++) {
    const value = encodeUserAssertion({ sub: "x".repeat(n), secret: SECRET, now: 1_700_000_000 + n });
    assert.doesNotMatch(value, /[=+/]/, `sub length ${n}`);
    assert.match(value, /^v1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{43}$/);
  }
  assert.equal(b64url(new Uint8Array([0xfb, 0xff, 0xfe])), "-__-");
  assert.equal(b64url("a"), "YQ");
});

test("iat defaults to the current time and the lifetime stays within the backend limit", () => {
  const before = Math.floor(Date.now() / 1000);
  const value = encodeUserAssertion({ sub: "u1", secret: SECRET });
  const parsed = JSON.parse(Buffer.from(value.split(".")[1], "base64url").toString("utf8"));
  assert.ok(parsed.iat >= before && parsed.iat <= before + 2);
  assert.ok(parsed.exp - parsed.iat <= MAX_TTL_SECONDS);
});

test("bad input throws and the message never contains the secret", () => {
  const cases: Array<() => unknown> = [
    () => encodeUserAssertion({ sub: "", secret: SECRET }),
    () => encodeUserAssertion({ sub: "has space", secret: SECRET }),
    () => encodeUserAssertion({ sub: "u1", secret: "too-short" }),
    () => encodeUserAssertion({ sub: "u1", secret: SECRET, ttlSeconds: 601 }),
    () => encodeUserAssertion({ sub: "u1", secret: SECRET, ttlSeconds: 0 }),
  ];
  for (const run of cases) {
    assert.throws(run, (err: Error) => {
      assert.ok(err instanceof RangeError);
      assert.ok(!err.message.includes(SECRET));
      return true;
    });
  }
});

test("usableSecret mirrors the backend: 32 or more characters, else unset", () => {
  assert.equal(usableSecret(undefined), null);
  assert.equal(usableSecret(""), null);
  assert.equal(usableSecret("x".repeat(31)), null);
  assert.equal(usableSecret("x".repeat(32)), "x".repeat(32));
});

test("isValidUserId accepts the backend's principal alphabet only", () => {
  for (const ok of ["u1", "auth0|abc", "a.b@c.d", "ns:id", "x".repeat(128)]) assert.equal(isValidUserId(ok), true, ok);
  for (const bad of ["", " ", "a b", "x".repeat(129), "a/b", "a\nb", null, 7]) assert.equal(isValidUserId(bad), false, String(bad));
});

test("devUserId is null in production and for unset or malformed ids", () => {
  assert.equal(devUserId("production", "dev-user"), null);
  assert.equal(devUserId("development", "dev-user"), "dev-user");
  assert.equal(devUserId("development", " dev-user "), "dev-user");
  assert.equal(devUserId("test", "dev-user"), "dev-user");
  assert.equal(devUserId(undefined, "dev-user"), "dev-user");
  assert.equal(devUserId("development", undefined), null);
  assert.equal(devUserId("development", ""), null);
  assert.equal(devUserId("development", "not valid!"), null);
});
