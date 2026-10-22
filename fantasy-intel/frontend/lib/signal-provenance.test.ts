import assert from "node:assert/strict";
import { test } from "node:test";
import {
  PROVENANCE_CONTRACT, ageText, coverageText, mergeSignals, provenanceStatus, signalsOf, summarizeProvenance,
  type SignalProvenance,
} from "./signal-provenance.ts";

const NOW = Date.parse("2026-10-22T12:00:00Z");
const hoursAgo = (h: number) => new Date(NOW - h * 36e5).toISOString();
const sig = (over: Partial<SignalProvenance> = {}): SignalProvenance => ({
  signal: "projections", label: "ESPN weekly projections", source: "espn", captured_at: hoursAgo(2),
  snapshot_version: "v1", coverage: null, reason_unavailable: null, stale_reason: null, stale_after_hours: 36, ...over,
});

test("a signal is current only with a known, recent capture time and nothing missing", () => {
  assert.equal(provenanceStatus(sig(), NOW), "current");
  assert.equal(provenanceStatus(sig({ captured_at: hoursAgo(37) }), NOW), "stale");
  assert.equal(provenanceStatus(sig({ captured_at: null }), NOW), "unknown");
  assert.equal(provenanceStatus(sig({ captured_at: "not a time" }), NOW), "unknown");
  // naive backend timestamps are UTC, as everywhere else in the app
  assert.equal(provenanceStatus(sig({ captured_at: "2026-10-22T11:00:00" }), NOW), "current");
});

test("source conflicts: a newer failed sync or a re-synced roster is stale whatever the age", () => {
  assert.equal(provenanceStatus(sig({ stale_reason: "The newest ESPN sync did not succeed." }), NOW), "stale");
});

test("partial data and missing values: counted as missing, never as zero", () => {
  const partial = sig({ coverage: { available: 8, total: 9, label: "starters with a projection" } });
  assert.equal(provenanceStatus(partial, NOW), "partial");
  assert.equal(coverageText(partial.coverage), "8 of 9 starters with a projection");
  assert.equal(provenanceStatus(sig({ coverage: { available: 0, total: 9, label: "x" } }), NOW), "unavailable");
  assert.equal(provenanceStatus(sig({ reason_unavailable: "No reports stored" }), NOW), "unavailable");
  assert.equal(provenanceStatus(sig({ reason_unavailable: "1 missing", coverage: { available: 8, total: 9, label: "x" } }), NOW), "partial");
  // unavailable outranks an unknown age; an unknown age outranks partial coverage
  assert.equal(provenanceStatus(sig({ captured_at: null, reason_unavailable: "no run" }), NOW), "unavailable");
  assert.equal(provenanceStatus(sig({ captured_at: null, coverage: { available: 8, total: 9, label: "x" } }), NOW), "unknown");
  assert.equal(coverageText(null), null);
});

test("ages never read as live", () => {
  assert.equal(ageText(null), "age unknown");
  assert.equal(ageText(0), "under 1h ago");
  assert.equal(ageText(5.9), "5h ago");
  assert.equal(ageText(72), "3d ago");
  // a capture time slightly in the future (clock skew) is not negative
  assert.equal(summarizeProvenance([sig({ captured_at: hoursAgo(-0.1) })], NOW)!.rows[0].age, "under 1h ago");
});

test("the badge names the worst status and the signal behind it, in words", () => {
  const league = sig({ signal: "league", label: "ESPN rosters", captured_at: hoursAgo(50) });
  const proj = sig({ coverage: { available: 8, total: 9, label: "starters with a projection" } });
  const s = summarizeProvenance([proj, league], NOW)!;
  assert.equal(s.status, "stale");
  assert.equal(s.text, "Stale data · ESPN league data 2d ago");
  assert.equal(summarizeProvenance([proj], NOW)!.text, "Partial data · 8 of 9 starters with a projection");
  assert.equal(summarizeProvenance([sig({ source: null, captured_at: null })], NOW)!.text, "Data age unknown · Projections");
  // current: the oldest input's age, never the freshest
  const cur = summarizeProvenance([sig({ captured_at: hoursAgo(1.5) }), sig({ signal: "league", captured_at: hoursAgo(20) })], NOW)!;
  assert.equal(cur.text, "Current data · oldest 20h ago");
  assert.equal(summarizeProvenance([sig({ signal: "injuries", source: "nflverse", reason_unavailable: "none stored" })], NOW)!.text,
    "Data unavailable · nflverse injury reports");
  assert.equal(summarizeProvenance([], NOW), null);
});

test("payloads: an unknown contract or a missing block yields no signals; merging keeps each signal once", () => {
  assert.deepEqual(signalsOf(null), []);
  assert.deepEqual(signalsOf({ contract: "something-else", signals: [sig()] }), []);
  const a = { contract: PROVENANCE_CONTRACT, signals: [sig({ signal: "league", label: "ESPN rosters" }), sig()] };
  const b = { contract: PROVENANCE_CONTRACT, signals: [sig({ signal: "league", label: "ESPN rosters" }), sig({ signal: "analysis", label: "Waivers", source: "fantasy_intel" })] };
  assert.deepEqual(mergeSignals(a, null, b).map((s) => s.signal), ["league", "projections", "analysis"]);
});
