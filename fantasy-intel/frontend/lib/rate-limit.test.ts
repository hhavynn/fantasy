import assert from "node:assert/strict";
import { test } from "node:test";
import { FixedWindowLimiter, clientIp, limitFromEnv } from "./rate-limit.ts";

test("a client gets `limit` requests per window, then a 429 with the seconds left", () => {
  const limiter = new FixedWindowLimiter(3, 60_000);
  for (let i = 0; i < 3; i++) assert.deepEqual(limiter.check("1.2.3.4", 1_000 + i), { ok: true });
  assert.deepEqual(limiter.check("1.2.3.4", 31_000), { ok: false, retryAfterSeconds: 30 });
  assert.deepEqual(limiter.check("5.6.7.8", 31_000), { ok: true }, "other clients are counted on their own");
});

test("the window resets", () => {
  const limiter = new FixedWindowLimiter(1, 10_000);
  assert.equal(limiter.check("a", 0).ok, true);
  assert.equal(limiter.check("a", 9_999).ok, false);
  assert.equal(limiter.check("a", 10_000).ok, true);
});

test("memory stays bounded: finished windows go first, then the oldest clients", () => {
  const limiter = new FixedWindowLimiter(5, 1_000, 3);
  limiter.check("a", 0);
  limiter.check("b", 500);
  limiter.check("c", 600);
  limiter.check("d", 1_200);                         // "a" has finished: it makes room
  assert.equal(limiter.size, 3);
  limiter.check("e", 1_300);                         // nothing finished: the oldest ("b") goes
  assert.equal(limiter.size, 3);
  for (let i = 0; i < 1_000; i++) limiter.check(`k${i}`, 1_400);
  assert.ok(limiter.size <= 3);
});

test("the client IP is the first x-forwarded-for entry, else x-real-ip, else unknown", () => {
  const headers = (h: Record<string, string>) => (n: string) => h[n] ?? null;
  assert.equal(clientIp(headers({ "x-forwarded-for": "9.9.9.9, 10.0.0.1", "x-real-ip": "8.8.8.8" })), "9.9.9.9");
  assert.equal(clientIp(headers({ "x-real-ip": "8.8.8.8" })), "8.8.8.8");
  assert.equal(clientIp(headers({})), null);
});

test("the limit is off unless RATE_LIMIT_PER_MINUTE is a positive whole number", () => {
  assert.equal(limitFromEnv(undefined), null);
  assert.equal(limitFromEnv(""), null);
  assert.equal(limitFromEnv("0"), null);
  assert.equal(limitFromEnv("-5"), null);
  assert.equal(limitFromEnv("2.5"), null);
  assert.equal(limitFromEnv("abc"), null);
  assert.equal(limitFromEnv(" 300 "), 300);
});
