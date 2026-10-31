import assert from "node:assert/strict";
import { test } from "node:test";
import { outcomeFor, writeClipboard } from "./name-free-copy.ts";

test("a copy outcome describes only the summary that was copied, never the next team, week or phase", () => {
  const week5 = "Fantasy Intel · Week 5 · 2026";
  const week6 = "Fantasy Intel · Week 6 · 2026";
  assert.equal(outcomeFor(null, week5), null);
  assert.equal(outcomeFor({ text: week5, outcome: "copied" }, week5), "copied");
  // the page re-rendered for another context with the same mounted control: the earlier "copied" is not shown
  assert.equal(outcomeFor({ text: week5, outcome: "copied" }, week6), null);
  assert.equal(outcomeFor({ text: week5, outcome: "unavailable" }, week6), null);
});

test("clipboard writes report copied, and a missing or refusing clipboard reports unavailable without throwing", async () => {
  const written: string[] = [];
  assert.equal(await writeClipboard("summary", { writeText: async (t) => { written.push(t); } }), "copied");
  assert.deepEqual(written, ["summary"]);
  assert.equal(await writeClipboard("summary", undefined), "unavailable");
  assert.equal(await writeClipboard("summary", null), "unavailable");
  assert.equal(await writeClipboard("summary", {}), "unavailable");
  assert.equal(await writeClipboard("summary", { writeText: () => Promise.reject(new Error("NotAllowedError")) }), "unavailable");
  assert.equal(await writeClipboard("summary", { writeText: () => { throw new Error("sync throw"); } }), "unavailable");
});
