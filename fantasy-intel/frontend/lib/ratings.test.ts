import assert from "node:assert/strict";
import { test } from "node:test";
import {
  RATING_SCALE_VERSION, compareRatings, formatRating, ratingFor, ratingSortKey, type Rating,
} from "./ratings.ts";

const r100 = (value: number): Rating => ({ scale: 100, value });
const legacy = (value: number): Rating => ({ scale: 10, value, legacy: true });

test("scale version", () => assert.equal(RATING_SCALE_VERSION, "ratings-100.v1"));

test("formatRating: one decimal with /100", () => {
  assert.equal(formatRating(r100(87.4)), "87.4/100");
  assert.equal(formatRating(r100(61.2)), "61.2/100");
  assert.equal(formatRating(r100(93.8)), "93.8/100");
  assert.equal(formatRating(r100(87)), "87.0/100");
});

test("formatRating: 0.0 and 100.0 are real ratings", () => {
  assert.equal(formatRating(r100(0)), "0.0/100");
  assert.equal(formatRating(r100(100)), "100.0/100");
});

test("formatRating: rounds to one decimal (87.45 is not a stored value, but must still format to one dp)", () => {
  assert.match(formatRating(r100(87.45))!, /^87\.[45]\/100$/);
  assert.equal(formatRating(r100(87.449)), "87.4/100");
  assert.equal(formatRating(r100(87.46)), "87.5/100");
});

test("formatRating: null is null", () => {
  assert.equal(formatRating(null), null);
  assert.equal(formatRating(undefined), null);
});

test("ratingFor: prefers the 0–100 field and ignores the legacy value", () => {
  const p = { opportunity_score_100: 87.4, opportunity_score: 8.7 };
  assert.deepEqual(ratingFor(p, "opportunity"), { scale: 100, value: 87.4 });
});

test("ratingFor: 0.0 is a rating, not missing", () => {
  assert.deepEqual(ratingFor({ production_score_100: 0, production_score: 0.1 }, "production"), { scale: 100, value: 0 });
});

test("ratingFor: null and absent 0–100 fields fall back to an explicit legacy value", () => {
  assert.deepEqual(ratingFor({ value_score_100: null, value_score: 6.1 }, "value"), { scale: 10, value: 6.1, legacy: true });
  assert.deepEqual(ratingFor({ value_score: 6.1 }, "value"), { scale: 10, value: 6.1, legacy: true });
});

test("ratingFor: both missing, NaN, null source and each kind read their own fields", () => {
  assert.equal(ratingFor({ value_score_100: null, value_score: null }, "value"), null);
  assert.equal(ratingFor({}, "opportunity"), null);
  assert.equal(ratingFor({ value_score_100: NaN, value_score: NaN }, "value"), null);
  assert.equal(ratingFor(null, "value"), null);
  assert.equal(ratingFor(undefined, "value"), null);
  const p = { opportunity_score_100: 10, production_score_100: 20, value_score_100: 30 };
  assert.equal(ratingFor(p, "opportunity")?.value, 10);
  assert.equal(ratingFor(p, "production")?.value, 20);
  assert.equal(ratingFor(p, "value")?.value, 30);
  assert.equal(ratingFor({ opportunity_score_100: 10 }, "value"), null);
});

test("legacy fallback is labelled and never shown as /100", () => {
  const r = ratingFor({ opportunity_score: 8.7 }, "opportunity");
  assert.equal(formatRating(r), "8.7/10 (legacy)");
  assert.ok(!formatRating(r)!.includes("/100"));
  assert.notEqual(formatRating(r), "8.7/100");
  assert.notEqual(formatRating(r), "87.0/100");
});

test("ratingSortKey: only 0–100 values are sortable", () => {
  assert.equal(ratingSortKey(r100(0)), 0);
  assert.equal(ratingSortKey(legacy(9.9)), null);
  assert.equal(ratingSortKey(null), null);
});

test("compareRatings: orders by 0–100 value, missing and legacy-only last in both directions", () => {
  const rows: { id: string; r: Rating | null }[] = [
    { id: "a", r: null }, { id: "b", r: legacy(9.9) }, { id: "c", r: r100(40) },
    { id: "d", r: r100(0) }, { id: "e", r: r100(92.5) }, { id: "f", r: null },
  ];
  const order = (dir: "asc" | "desc") => [...rows].sort((x, y) => compareRatings(dir)(x.r, y.r)).map((x) => x.id).join("");
  assert.equal(order("desc"), "ecdabf");
  assert.equal(order("asc"), "dceabf");
});

test("compareRatings: a legacy 9.9 does not outrank a 0–100 rating of 5", () => {
  assert.ok(compareRatings("desc")(r100(5), legacy(9.9)) < 0);
  assert.ok(compareRatings("asc")(r100(5), legacy(9.9)) < 0);
  assert.equal(compareRatings("desc")(legacy(1), null), 0);
});
