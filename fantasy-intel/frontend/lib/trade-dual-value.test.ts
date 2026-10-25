// Run: npm test (node --test with type stripping).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import {
  CRITERIA_ORDER, CRITERION_SHORT, DEFAULT_MODE, LENSES, VERDICT_ORDER, beforeAfterText, checkStatusInfo, checkTitle, concernLabel, concernSource,
  excludedText, favoursText, isLegacyMode, lensTabText, lensTabs, modeUsesPhase, modeUsesStrictness, orderedChecks, playerLabel, pointsText,
  ratingFields, ratingText, ratioText, parText, roleChangeText, weeksText, sectionsAreVerdicts, verdictInfo, verdictRank, weeklyChangeRow,
} from "./trade-dual-value.ts";
import { PAGE_DEFAULT_MODE, VERDICT_RANK_ORDER } from "./trade-discovery.ts";
import type { DEvidence, DiscoverySummary, DSummaryRow } from "./trade-types.ts";

const fixture = JSON.parse(readFileSync(new URL("./fixtures/trade-discovery-dual-value.synthetic.json", import.meta.url), "utf8")) as {
  summary: DiscoverySummary; evidence: DEvidence;
};
const rows: DSummaryRow[] = Object.values(fixture.summary.sections).flat();
const byVerdict = (v: string) => rows.find((r) => r.dual_value_summary?.verdict === v)!;

// --------------------------------------------------------------------------------------- lenses --

test("the lens tabs are the seven lenses in the documented order, mutual upgrades first", () => {
  assert.deepEqual(LENSES.map((l) => l.mode), ["mutual_upgrades", "fair", "upside", "consolidation", "playoff", "targeted", "all"]);
  assert.deepEqual(LENSES.map((l) => l.label), [
    "Best mutual upgrades", "Fairer asset exchanges", "Maximum upside", "Consolidation deals", "Playoff-focused trades", "Targeted search",
    "All candidates",
  ]);
  assert.equal(DEFAULT_MODE, "mutual_upgrades");
  assert.equal(LENSES[0].mode, DEFAULT_MODE);
  assert.equal(lensTabText(LENSES[6]), "All candidates · advanced");
  assert.equal(lensTabText(LENSES[0]), "Best mutual upgrades");
});

test("a legacy mode adds one extra classic tab; a lens adds none", () => {
  for (const m of ["mutual_upgrades", "fair", "upside", "consolidation", "playoff", "targeted", "all", undefined, "bogus"]) {
    assert.equal(lensTabs(m).length, 7, `${m}`);
  }
  const practical = lensTabs("practical");
  assert.equal(practical.length, 8);
  assert.deepEqual(practical.at(-1), { mode: "practical", label: "Practical (classic)" });
  assert.deepEqual(lensTabs("mutual").at(-1), { mode: "mutual", label: "Mutual improvement (classic)" });
  assert.equal(isLegacyMode("practical"), true);
  assert.equal(isLegacyMode("mutual"), true);
  assert.equal(isLegacyMode("mutual_upgrades"), false);
  assert.equal(isLegacyMode("toString"), false);
  assert.equal(isLegacyMode(null), false);
});

test("the phase filter is hidden for upside, all and playoff; strictness is practical-only", () => {
  for (const m of ["upside", "all", "playoff"]) assert.equal(modeUsesPhase(m), false, m);
  for (const m of ["mutual_upgrades", "fair", "consolidation", "practical", "mutual"]) assert.equal(modeUsesPhase(m), true, m);
  assert.equal(modeUsesStrictness("practical"), true);
  for (const m of ["mutual", "mutual_upgrades", "fair", "all"]) assert.equal(modeUsesStrictness(m), false, m);
});

// ------------------------------------------------------------------------------------- verdicts --

test("verdict tone and label follow the contract; an unknown verdict stays neutral", () => {
  assert.deepEqual(VERDICT_ORDER, Object.keys(fixture.summary.verdicts!));
  const tone = Object.fromEntries(VERDICT_ORDER.map((v) => [v, verdictInfo(v).tone]));
  assert.deepEqual(tone, {
    sensible: "good", sensible_with_concerns: "warn", unlikely_mutual: "bad", fair_no_lineup_gain: "warn", one_sided: "bad", incomplete: "",
  });
  for (const v of VERDICT_ORDER) assert.ok(verdictInfo(v).label.length > 0);
  assert.deepEqual(verdictInfo("something_new"), { label: "something new", tone: "" });
  assert.deepEqual(verdictInfo(null), { label: "—", tone: "" });
  assert.equal(verdictInfo("one_sided").label, "One-sided");
});

test("verdict rank follows the backend order; missing or unknown has no rank", () => {
  assert.deepEqual(VERDICT_ORDER.map(verdictRank), [0, 1, 2, 3, 4, 5]);
  assert.equal(verdictRank(undefined), null);
  assert.equal(verdictRank("nope"), null);
});

test("no verdict, label or section text promises acceptance", () => {
  const all = [...Object.values(fixture.summary.verdicts!), ...VERDICT_ORDER.map((v) => verdictInfo(v).label), fixture.summary.ranking_policy!];
  for (const t of all) assert.doesNotMatch(t, /likely to accept|chance|probab|% /i, t);
});

// --------------------------------------------------------------------------------------- checks --

test("each check status has a glyph, a word and a tone, so colour is never the only signal", () => {
  assert.deepEqual(checkStatusInfo("pass"), { glyph: "✓", label: "Pass", tone: "good" });
  assert.deepEqual(checkStatusInfo("concern"), { glyph: "!", label: "Concern", tone: "warn" });
  assert.deepEqual(checkStatusInfo("fail"), { glyph: "✕", label: "Fail", tone: "bad" });
  assert.deepEqual(checkStatusInfo("unknown"), { glyph: "?", label: "Unknown", tone: "" });
  assert.deepEqual(checkStatusInfo("not_applicable"), { glyph: "–", label: "Not applicable", tone: "" });
  const glyphs = ["pass", "concern", "fail", "unknown", "not_applicable"].map((s) => checkStatusInfo(s).glyph);
  assert.equal(new Set(glyphs).size, 5);
  assert.equal(checkStatusInfo("brand_new").label, "Unknown");          // a newer status never crashes the page
  assert.equal(checkStatusInfo(undefined).glyph, "?");
});

test("the six criteria are chipped in contract order whatever order they arrive in", () => {
  const shuffled = [...byVerdict("sensible").dual_value_summary!.checks].reverse();
  assert.deepEqual(orderedChecks(shuffled).map((c) => c.criterion), [...CRITERIA_ORDER]);
  assert.deepEqual(orderedChecks(undefined), []);
  for (const c of CRITERIA_ORDER) assert.ok(CRITERION_SHORT[c], c);
  assert.deepEqual(Object.keys(fixture.summary.criteria!), [...CRITERIA_ORDER]);
});

test("a chip's title names the criterion, the status and what it means", () => {
  const c = { criterion: "asset_balance", status: "concern" as const };
  assert.equal(checkTitle(c, fixture.summary.criteria), "Assets: Concern. The asset packages are reasonably balanced (points above replacement).");
  assert.match(checkTitle(c, null), /^Assets: Concern\. The asset packages/);           // falls back to the built-in text
  assert.equal(checkTitle({ ...c, text: "They give more." }, fixture.summary.criteria), "Assets: Concern. They give more.");
  assert.equal(checkTitle({ criterion: "future_check", status: "pass" }), "future check: Pass.");
});

// ------------------------------------------------------------------------ balance and concern --

test("favours text says which manager the imbalance favours, and nothing when balanced", () => {
  assert.equal(favoursText("You"), "Favours you");
  assert.equal(favoursText("They"), "Favours them");
  assert.equal(favoursText("Gridiron Goblins"), "Favours Gridiron Goblins");
  assert.equal(favoursText(null), null);
  assert.equal(favoursText(undefined), null);
  assert.equal(favoursText(""), null);
  assert.equal(favoursText(byVerdict("sensible").dual_value_summary!.asset.favours), null);
  assert.equal(favoursText(byVerdict("sensible_with_concerns").dual_value_summary!.asset.favours), "Favours you");
});

test("the concern is a fairness concern when the asset check raises it, otherwise a downside", () => {
  const v = (id: string) => byVerdict(id).dual_value_summary!;
  assert.equal(concernLabel(v("sensible_with_concerns").checks, v("sensible_with_concerns").main_concern), "Fairness concern");
  assert.equal(concernLabel(v("unlikely_mutual").checks, v("unlikely_mutual").main_concern), "Fairness concern");
  assert.equal(concernLabel(v("sensible").checks, null), null);
  // A failed lineup check comes first, so an asset concern beside it does not turn the text into a fairness concern.
  const lineupFail = [{ criterion: "lineup_gain", status: "fail" as const }, { criterion: "asset_balance", status: "concern" as const }];
  assert.equal(concernSource(lineupFail), "lineup_gain");
  assert.equal(concernLabel(lineupFail, "Their starting lineup does not improve."), "Downside");
  // A depth concern with a passing asset check is a downside.
  const depth = [{ criterion: "asset_balance", status: "pass" as const }, { criterion: "depth_cost", status: "concern" as const }];
  assert.equal(concernLabel(depth, "They must drop a starter."), "Downside");
  // Fail beats concern, and a concern earlier in the order does not beat a later fail.
  const mixed = [{ criterion: "asset_balance", status: "concern" as const }, { criterion: "depth_cost", status: "fail" as const }];
  assert.equal(concernSource(mixed), "depth_cost");
  // Older backend: no checks, still a concern text.
  assert.equal(concernLabel(undefined, "Something to weigh."), "Downside");
  assert.equal(concernLabel([], ""), null);
});

// -------------------------------------------------------------------------------------- numbers --

test("starting points show before and after, with a missing end as a dash and never 0", () => {
  assert.equal(pointsText(1210.34), "1,210.3");
  assert.equal(pointsText(0), "0.0");                        // a measured zero stays a zero
  assert.equal(pointsText(null), "—");
  assert.equal(pointsText(undefined), "—");
  assert.equal(pointsText(Number.NaN), "—");
  const you = byVerdict("sensible").dual_value_summary!.lineup.you;
  assert.equal(beforeAfterText(you.before.ros, you.after.ros), "1,210.3 → 1,224.5");
  const incomplete = byVerdict("incomplete").dual_value_summary!.lineup.you;
  assert.equal(beforeAfterText(incomplete.before.ros, incomplete.after.ros), "1,200.0 → —");
});

test("ratings read xx.x/100 and a missing rating is a dash, never 0", () => {
  assert.equal(ratingText(88), "88.0/100");
  assert.equal(ratingText(0), "0.0/100");                    // a real zero rating is shown as a zero
  assert.equal(ratingText(null), "—");
  assert.equal(ratingText(undefined), "—");
  const r = fixture.evidence.ratings!.players;
  assert.deepEqual(ratingFields(r["205"]).map((f) => f.text), ["88.0/100", "79.5/100", "84.0/100"]);
  assert.deepEqual(ratingFields(r["204"]).map((f) => f.text), ["33.0/100", "—", "—"]);
  assert.deepEqual(ratingFields(null).map((f) => f.text), ["—", "—", "—"]);
  assert.deepEqual(ratingFields(undefined).map((f) => f.label), ["Opportunity", "Production", "Value"]);
});

test("ratings are only paired within one position and pool", () => {
  const e = fixture.evidence.ratings!;
  assert.deepEqual(e.comparable_pairs, [{ you_send: 203, you_receive: 205, position: "RB" }]);
  const pos = (id: number) => e.players[String(id)]?.position;
  for (const p of e.comparable_pairs) assert.equal(pos(p.you_send), pos(p.you_receive));
});

// ---------------------------------------------------------------------------- weekly lineup rows --

test("a weekly change row resolves names, falls back to #id and shows a missing delta as a dash", () => {
  const dv = fixture.evidence.dual_value!;
  const names = fixture.evidence.names;
  const you = dv.lineup.you.weekly.map((w) => weeklyChangeRow(w, names));
  assert.deepEqual(you[0], { week: 11, phase: "regular season", delta: 2.4, deltaText: "+2.4", entered: ["Marcus Vale"], exited: ["Milo Strand"], changed: true });
  assert.equal(you[3].phase, "playoffs");
  assert.deepEqual(you[3].exited, ["Milo Strand", "#299"]);        // #299 has no entry in names
  assert.deepEqual(you[2], { week: 13, phase: "regular season", delta: 0, deltaText: "0.0", entered: [], exited: [], changed: false });
  const them = dv.lineup.them.weekly.map((w) => weeklyChangeRow(w, names));
  assert.equal(them[3].delta, null);
  assert.equal(them[3].deltaText, "—");                            // missing is never 0
  assert.equal(weeklyChangeRow({ week: 9, phase: "regular", delta: -1.26, entered: [], exited: [] }, names).deltaText, "−1.3");
  assert.equal(weeklyChangeRow({ week: 9, phase: null, delta: undefined as unknown as number, entered: [7], exited: [] }, undefined).deltaText, "—");
  assert.deepEqual(weeklyChangeRow({ week: 9, phase: null, delta: null, entered: [7], exited: [] }, undefined).entered, ["#7"]);
  assert.equal(playerLabel(203, names), "Milo Strand");
  assert.equal(playerLabel(1, names), "#1");
  assert.equal(playerLabel(1, null), "#1");
  assert.equal(playerLabel(5, { "5": { name: null } }), "#5");
});

test("the weekly rows for both sides cover every week and agree with the lineup delta", () => {
  const { you, them, weeks } = fixture.evidence.dual_value!.lineup;
  assert.equal(you.weekly.length, weeks);
  assert.equal(them.weekly.length, weeks);
  // A known weekly delta is part of the side total; the one missing week is flagged incomplete, not counted as 0.
  const known = (ws: typeof you.weekly) => ws.filter((w) => w.delta !== null).reduce((s, w) => s + (w.delta as number), 0);
  assert.equal(known(you.weekly).toFixed(1), "6.5");
  assert.equal(known(them.weekly).toFixed(1), "5.1");
  assert.equal(them.missing.weeks_incomplete, 1);
});

// ------------------------------------------------------------------------------- sections, rows --

test("verdict sections are recognised; classic tiers and results are not", () => {
  assert.equal(sectionsAreVerdicts(Object.keys(fixture.summary.sections), fixture.summary.verdicts), true);
  assert.equal(sectionsAreVerdicts(["practical", "concerns", "speculative"], fixture.summary.verdicts), false);
  assert.equal(sectionsAreVerdicts(["results"], fixture.summary.verdicts), false);
  assert.equal(sectionsAreVerdicts(["practical", "concerns"], undefined), false);
  assert.equal(sectionsAreVerdicts([], fixture.summary.verdicts), false);
  assert.equal(sectionsAreVerdicts(["sensible"], undefined), true);
  assert.equal(sectionsAreVerdicts(["new_verdict"], { new_verdict: "A newer verdict." }), true);
});

test("the excluded counts read as a list and leave out zeros", () => {
  assert.equal(excludedText(fixture.summary.excluded), "41 not mutual");
  assert.equal(excludedText({ lopsided: 3, one_sided: 1, similar_variants: 0 }), "3 lopsided, 1 one sided");
  assert.equal(excludedText({}), "");
  assert.equal(excludedText(undefined), "");
});

test("the fixture's rows carry what the table renders; a legacy row without dual value is still a valid row", () => {
  assert.equal(rows.length, 4);
  for (const r of rows) {
    const s = r.dual_value_summary!;
    assert.ok(verdictRank(s.verdict) !== null);
    assert.equal(s.checks.length, 6);
    assert.ok(s.you_change);
    assert.equal(r.sends.length > 0 && r.receives.length > 0, true);
  }
  assert.equal(byVerdict("incomplete").mine.ros_delta, null);               // missing stays null
  assert.equal(byVerdict("incomplete").dual_value_summary!.main_concern, null);
  const legacy: DSummaryRow = { ...rows[0], dual_value_summary: undefined };
  assert.equal(verdictRank(legacy.dual_value_summary?.verdict), null);
  assert.deepEqual(orderedChecks(legacy.dual_value_summary?.checks), []);
});

test("the evidence assessment is internally consistent: verdict, checks and asset block agree with the summary row", () => {
  const dv = fixture.evidence.dual_value!;
  const row = byVerdict("sensible_with_concerns");
  assert.equal(dv.plausibility.verdict, row.dual_value_summary!.verdict);
  assert.equal(dv.plausibility.main_concern, row.dual_value_summary!.main_concern);
  assert.deepEqual(dv.plausibility.checks.map((c) => [c.criterion, c.status]), row.dual_value_summary!.checks.map((c) => [c.criterion, c.status]));
  assert.equal(dv.asset.favours, row.dual_value_summary!.asset.favours);
  assert.equal(dv.projection_source, "espn");
  assert.equal(fixture.summary.projection_source!.experimental_comparison, null);
  // A bench asset keeps its asset value (par) but never counts as starting: it starts nowhere for the receiver.
  const bench = dv.asset.you_send.find((p) => p.bench_asset)!;
  assert.equal(bench.starts_after, 0);
  assert.ok(bench.par! > 0);
  assert.deepEqual(dv.asset.bench_assets, [bench.espn_player_id]);
});

test("asset and role text: unknown values are dashes, a forced drop is named, weeks are listed", () => {
  assert.equal(ratioText(0.64), "64%");
  assert.equal(ratioText(0), "0%");                         // a measured ratio of zero is a number
  assert.equal(ratioText(null), "—");
  assert.equal(parText(38), "38.0");
  assert.equal(parText(0), "0.0");
  assert.equal(parText(null), "—");
  assert.equal(weeksText([11, 12]), "wk 11, 12");
  assert.equal(weeksText([]), "—");
  assert.equal(weeksText(undefined), "—");
  const [strand, vasko] = fixture.evidence.dual_value!.asset.you_send;
  assert.deepEqual(roleChangeText(strand), { roles: "part-time → starter", starts: "2/4 → 3/4 weeks" });
  assert.deepEqual(roleChangeText(vasko), { roles: "bench → bench", starts: "0/4 → 0/4 weeks" });
  assert.deepEqual(roleChangeText({ role_before: "starter", role_after: null, starts_before: 3, starts_after: null, weeks: 4 }),
    { roles: "starter → forced drop", starts: "3/4 → — weeks" });
  assert.deepEqual(roleChangeText({ role_before: null, role_after: null, starts_before: null, starts_after: null, weeks: 0 }),
    { roles: "— → forced drop", starts: "— → — weeks" });
});

test("trade-discovery.ts repeats the default lens and the verdict order exactly", () => {
  assert.equal(PAGE_DEFAULT_MODE, DEFAULT_MODE);
  assert.deepEqual([...VERDICT_RANK_ORDER], [...VERDICT_ORDER]);
});
