import assert from "node:assert/strict";
import { test } from "node:test";
import {
  MAX_CHARS, askHref, askPath, isCard, isSample, linkHref, noAnswerTitle, readQuestion, sampleText, valueText,
  type AskCard,
} from "./ask.ts";

test("the question is read from ?q=, trimmed, and flagged when past the backend's bound", () => {
  assert.deepEqual(readQuestion({}), { q: "", tooLong: false });
  assert.deepEqual(readQuestion({ q: "  who should I start  " }), { q: "who should I start", tooLong: false });
  assert.deepEqual(readQuestion({ q: ["first", "second"] }), { q: "first", tooLong: false });
  assert.equal(readQuestion({ q: "a".repeat(MAX_CHARS) }).tooLong, false);
  assert.equal(readQuestion({ q: "a".repeat(MAX_CHARS + 1) }).tooLong, true);
});

test("backend paths and re-ask links encode the question and keep only an explicit team", () => {
  assert.equal(askPath("is Waiver RB available?"), "/api/ask?q=is+Waiver+RB+available%3F");
  assert.equal(askHref("how is papa wr (WR, PHI) doing", null), "/ask?q=how+is+papa+wr+%28WR%2C+PHI%29+doing");
  assert.equal(askHref("who should I start", 2), "/ask?q=who+should+I+start&team=2");
});

test("a card link becomes an in-app href with repeated params and the fragment", () => {
  assert.equal(linkHref({ path: "/start-sit", query: {} }), "/start-sit");
  assert.equal(linkHref({ path: "/waivers", query: { view: "BEST", pos: "RB", team: 2 } }),
    "/waivers?view=BEST&pos=RB&team=2");
  assert.equal(linkHref({ path: "/screener", query: { pos: ["WR", "TE"], fg: ["1", "2"], fm: ["targets_pg", "target_share"] } }),
    "/screener?pos=WR&pos=TE&fg=1&fg=2&fm=targets_pg&fm=target_share");
  assert.equal(linkHref({ path: "/schedule-intelligence", query: {}, hash: "bye-planner" }),
    "/schedule-intelligence#bye-planner");
});

test("values keep missing as missing and carry their unit", () => {
  assert.equal(valueText(null), null);
  assert.equal(valueText(null, "pts"), null);
  assert.equal(valueText(0, "pts"), "0 pts");                          // a measured zero is a zero
  assert.equal(valueText(74.7, "pts"), "74.7 pts");
  assert.equal(valueText(5, null), "5");
  assert.equal(valueText(2.5, "%"), "2.5%");
  assert.equal(valueText(true), "Yes");
  assert.equal(valueText("clear"), "clear");
});

test("screener samples read as games counted of games played", () => {
  assert.ok(isSample({ value: 25.3, n: 2, of: 3 }));
  assert.ok(!isSample("Papa WR") && !isSample(null) && !isSample(4));
  assert.equal(sampleText({ value: 25.3, n: 2, of: 3 }), "2 of 3 games");
  assert.equal(sampleText({ value: 25.3, n: 1, of: 1 }), "1 of 1 game");
  assert.equal(sampleText({ value: null, n: null, of: null }), null);
});

test("the contract is checked and an unanswered card says why in words", () => {
  const card = { contract: "ask/v1", status: "abstain", abstain: { code: "unsupported", reason: "x" } } as AskCard;
  assert.ok(isCard(card) && !isCard({ contract: "ask/v2" }) && !isCard(null));
  assert.equal(noAnswerTitle(card), "Ask can't answer that exactly");
  assert.equal(noAnswerTitle({ ...card, abstain: { code: "empty", reason: null } }), "Ask a question about this league");
  assert.equal(noAnswerTitle({ ...card, status: "unavailable" }), "The data for this answer is not available");
});
