import assert from "node:assert/strict";
import { test } from "node:test";
import { competitionRank, knownWaiverScore, needPartialReason, pickTopClaims, trendRank, waiverScoreGap, withParams } from "./waivers.ts";

type Row = { id: string; recommendation: string; components: { decision?: { actionable?: boolean } | null } };
const row = (id: string, recommendation: string, actionable?: boolean): Row => ({
  id, recommendation, components: { decision: actionable === undefined ? undefined : { actionable } },
});
const ids = (rows: Row[]) => rows.map((r) => r.id).join("");

test("trendRank orders rising above flat above falling, and leaves unknown unranked", () => {
  assert.equal(trendRank("UP"), 1);
  assert.equal(trendRank("FLAT"), 0);
  assert.equal(trendRank("DOWN"), -1);
  assert.equal(trendRank("INSUFFICIENT"), null);
  assert.equal(trendRank(null), null);
  assert.equal(trendRank(undefined), null);
});

test("competitionRank orders low, medium, high", () => {
  assert.equal(competitionRank("LOW"), 1);
  assert.equal(competitionRank("MEDIUM"), 2);
  assert.equal(competitionRank("HIGH"), 3);
  assert.equal(competitionRank("other"), null);
  assert.equal(competitionRank(null), null);
});

test("pickTopClaims: actionable rows come first, capped at three, in the given order", () => {
  const rows = [row("a", "WAIT", false), row("b", "CLAIM", true), row("c", "CLAIM", true), row("d", "CLAIM", true), row("e", "CLAIM", true)];
  const top = pickTopClaims(rows);
  assert.equal(top.mode, "actionable");
  assert.equal(ids(top.rows), "bcd");
});

test("pickTopClaims: with nothing actionable it falls back to non-IGNORE rows, labelled as watch", () => {
  const rows = [row("a", "IGNORE", false), row("b", "WAIT", false), row("c", "WAIT", false), row("d", "IGNORE", false)];
  const top = pickTopClaims(rows);
  assert.equal(top.mode, "watch");
  assert.equal(ids(top.rows), "bc");
});

test("pickTopClaims: a row without a decision is never actionable", () => {
  const top = pickTopClaims([row("a", "CLAIM"), row("b", "IGNORE")]);
  assert.equal(top.mode, "watch");
  assert.equal(ids(top.rows), "a");
});

test("pickTopClaims: all passes, or no rows, give none", () => {
  assert.equal(pickTopClaims([row("a", "IGNORE", false)]).mode, "none");
  assert.deepEqual(pickTopClaims([]), { mode: "none", rows: [] });
});

test("withParams: updates one param, keeps the rest, and drops nulls", () => {
  assert.equal(withParams({ view: "BEST", pos: "RB", sort: "score" }, { pos: "WR" }), "?view=BEST&sort=score&pos=WR");
  assert.equal(withParams({ view: "BEST", pos: "RB", offset: "30" }, { pos: null, offset: null }), "?view=BEST");
  assert.equal(withParams({}, { offset: null }), "?");
});

test("withParams: repeated params survive and undefined values are skipped", () => {
  assert.equal(withParams({ a: ["1", "2"], b: undefined }, { c: "3" }), "?a=1&a=2&c=3");
});

test("a waiver score missing its lineup-gain term is unknown, never a measured number", () => {
  const partial = { waiver_score: 3.2, components: { roster: { fit_status: "partial", value_unknown: [{ name: "Cand QB" }] } } };
  assert.equal(knownWaiverScore(partial), null);
  assert.match(waiverScoreGap(partial), /Cand QB/);
  assert.equal(knownWaiverScore({ waiver_score: 3.2, components: { roster: { fit_status: "complete" } } }), 3.2);
  assert.equal(knownWaiverScore({ waiver_score: 3.2, components: {} }), 3.2);   // older rows carry no status
  assert.equal(knownWaiverScore({ waiver_score: 3.2 }), 3.2);                    // a player-page row without components
});

test("a need without its starter-quality term is marked partial with the stored reason", () => {
  assert.equal(needPartialReason({ value_status: "complete" }), null);
  assert.equal(needPartialReason(undefined), null);
  assert.equal(needPartialReason({ value_status: "partial", value_note: "no value for P2" }), "no value for P2");
  assert.match(needPartialReason({ value_status: "partial" }) ?? "", /shortage only/);
});
