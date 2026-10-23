import assert from "node:assert/strict";
import { test } from "node:test";
import {
  eligibleText, groupRules, isLeagueRules, pointsText, ruleDetail, rosterSummary, rulesHeadline, statusCounts, statusLabel,
  unmappedValue, type LeagueRules, type RulesSlot, type ScoringRule,
} from "./league-rules.ts";

const missing = (reason: string) => ({ value: null, display: null, reason });
const known = <T,>(value: T, display: string) => ({ value, display, reason: null });

function slot(code: string, kind: RulesSlot["kind"], eligible: string[] | null): RulesSlot {
  return { code, name: code, count: 1, kind, eligible, eligible_reason: eligible ? null : "Not mapped.",
    engine: { status: "handled", note: "" } };
}

function rule(over: Partial<ScoringRule> = {}): ScoringRule {
  return { key: "53", group: "Receiving", label: "Reception", points: 1, by_slot: [], by_position: [], reverse: false,
    mapped: true, ...over };
}

function rules(over: Partial<LeagueRules> = {}): LeagueRules {
  return {
    contract: "league-rules/v1",
    source: { provider: "espn", provider_label: "ESPN", captured_at: null, note: "" },
    league: { name: "L", season: 2026, team_count: known(10, "10"), league_type: missing("Not synced.") },
    roster: { slots: [], starters: known(9, "9"), bench: 6, injured_reserve: 2, taxi: 0, superflex: true,
      flex_types: ["FLEX", "OP"], position_limits: missing("Not synced.") },
    scoring: { format: known("half_ppr", "Half PPR"), points_per_reception: known(0.5, "0.5"),
      te_premium: known(0.5, "+0.5"), te_premium_scope: "position", scoring_type: missing("None."), highlights: [],
      rules: [], zero_rules: 0, rules_source: "" },
    lineup_lock: missing("Not synced."),
    waivers: { type: known("faab", "FAAB bidding"), faab_budget: known(100, "$100"), period: missing("x"), process_days: missing("x") },
    trades: { deadline: known(11, "Week 11"), review: missing("x") },
    playoffs: { teams: known(4, "4"), final_week: missing("x"), regular_season: missing("x"), start: missing("x"),
      round_length: missing("x") },
    engine_support: [], unmapped: [],
    ...over,
  };
}

test("only the league-rules/v1 contract is accepted", () => {
  assert.equal(isLeagueRules(rules()), true);
  assert.equal(isLeagueRules({ ...rules(), contract: "league-rules/v2" }), false);
  assert.equal(isLeagueRules(null), false);
  assert.equal(isLeagueRules({ contract: "league-rules/v1" }), false);
});

test("engine statuses read as words with a tone, and an unknown status never reads as handled", () => {
  assert.deepEqual(statusLabel("handled"), { label: "Handled", tone: "good" });
  assert.deepEqual(statusLabel("unsupported"), { label: "Not applied", tone: "bad" });
  assert.deepEqual(statusLabel("something-new"), { label: "Unverified", tone: "info" });
  const counts = statusCounts([
    { code: "a", rule: "a", status: "unsupported", detail: "" }, { code: "b", rule: "b", status: "handled", detail: "" },
    { code: "c", rule: "c", status: "handled", detail: "" },
  ]);
  assert.deepEqual(counts, [{ status: "handled", count: 2 }, { status: "unsupported", count: 1 }]);
});

test("slot eligibility: positions for starters, the purpose for bench and reserve, null when unmapped", () => {
  assert.equal(eligibleText(slot("OP", "starter", ["QB", "RB", "WR", "TE"])), "QB, RB, WR, TE");
  assert.equal(eligibleText(slot("BE", "bench", [])), "Any rostered player");
  assert.match(eligibleText(slot("TAXI", "taxi", [])) ?? "", /taxi/);
  assert.equal(eligibleText(slot("ER", "unknown", null)), null);
});

test("points keep their sign and decimals", () => {
  assert.equal(pointsText(4), "+4");
  assert.equal(pointsText(0.04), "+0.04");
  assert.equal(pointsText(-2), "−2");
  assert.equal(pointsText(0), "0");
});

test("slot and position specific points are spelled out next to the rule", () => {
  assert.equal(ruleDetail(rule()), null);
  assert.equal(ruleDetail(rule({ by_slot: [{ slot: "TE", points: 1.5 }] })), "TE slot +1.5");
  assert.equal(ruleDetail(rule({ by_position: [{ position: "TE", points: 0.5 }], reverse: true })), "TE +0.5 extra · reverse rule");
});

test("rules are grouped in a fixed order with unmapped last", () => {
  const out = groupRules([rule({ group: "Unmapped", key: "x" }), rule({ group: "Kicking", key: "k" }),
    rule({ group: "Passing", key: "p" }), rule({ group: "Passing", key: "p2" })]);
  assert.deepEqual(out.map((g) => [g.group, g.rules.length]), [["Passing", 2], ["Kicking", 1], ["Unmapped", 1]]);
});

test("summary lines use only values the backend holds", () => {
  const d = rules();
  assert.equal(rosterSummary(d.roster), "9 starters · 6 bench · 2 IR");
  assert.equal(rulesHeadline(d), "Half PPR · TE premium · Superflex · FAAB waivers");
  const bare = rules({
    roster: { ...d.roster, starters: missing("No slots."), bench: 0, injured_reserve: 0, superflex: false },
    scoring: { ...d.scoring, format: missing("No rules."), te_premium: missing("No rules.") },
    waivers: { ...d.waivers, type: missing("Unknown.") },
  });
  assert.equal(rosterSummary(bare.roster), "");
  assert.equal(rulesHeadline(bare), "League rules as synced");
});

test("unmapped values are shown verbatim, and a withheld structured value stays missing", () => {
  assert.equal(unmappedValue(1), "1");
  assert.equal(unmappedValue(false), "false");
  assert.equal(unmappedValue("ROLLING"), "ROLLING");
  assert.equal(unmappedValue(null), null);
});
