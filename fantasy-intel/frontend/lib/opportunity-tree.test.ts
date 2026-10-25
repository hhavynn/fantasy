import assert from "node:assert/strict";
import { test } from "node:test";
import type { Roster } from "./injury-impact.ts";
import {
  CERTAINTY_LABEL, claimText, eligibilityHref, emptyEffectsText, evidenceText, personnelText, sampleText, sharePct, teamSummary,
  treeErrorText,
  type Effect, type Ownership, type Scenario, type TreePerson, type TreeTeam,
} from "./opportunity-tree.ts";

const roster = (over: Partial<Roster> = {}): Roster => ({
  status: "FREE_AGENT", fantasy_team_id: null, fantasy_team: null, is_mine: false, percent_owned: null, ...over,
});

const person = (own: Ownership, over: Partial<TreePerson> = {}): TreePerson => ({
  gsis_id: "g1", espn_player_id: 42, full_name: "Bench RB", position: "RB", depth_rank: 2,
  availability: { level: "AVAILABLE", known: true, sources: [] }, roster: roster(), ownership: own, ...over,
});

const effect = (basis: Effect["basis"]): Effect => ({
  ...person({ claim: "free_agent", claimable: true, needs_claim: false }), basis, reasons: [],
  observed: { with: null, without: null, delta_per_game: null }, estimate: null,
});

const scenario = (over: Partial<Scenario> = {}): Scenario => ({
  kind: "teammate_unavailable",
  subject: { ...person({ claim: "opponent", claimable: false, needs_claim: null }), absence: { last_played_week: 4, consecutive_team_games_missed: 2 } },
  certainty: "designated_out", evidence: [], unit: "touches", unit_label: "carries + targets",
  sample: { team_games_with: 4, team_games_without: 2, weeks_without: [5, 6] },
  outcome: "supported", abstain: null, effects: [], ...over,
});

test("an opponent's player is never claimable and gets no eligibility link", () => {
  const own: Ownership = { claim: "opponent", claimable: false, needs_claim: null };
  const c = claimText(own, roster({ status: "ROSTERED", fantasy_team: "Team Five", fantasy_team_id: 5 }), "ESPN");
  assert.equal(c.text, "Team Five");
  assert.match(c.detail, /not claimable/);
  assert.equal(eligibilityHref(person(own)), null);
  assert.equal(eligibilityHref(person({ claim: "rostered", claimable: false, needs_claim: null })), null);
  assert.equal(eligibilityHref(person({ claim: "yours", claimable: false, needs_claim: null })), null);
  assert.equal(eligibilityHref(person({ claim: "unknown", claimable: null, needs_claim: null })), null);
});

test("claimable players link to the add/drop eligibility check on the waivers page", () => {
  assert.equal(eligibilityHref(person({ claim: "waivers", claimable: true, needs_claim: true })), "/waivers?pos=RB&add=42#add-drop");
  assert.equal(eligibilityHref(person({ claim: "free_agent", claimable: true, needs_claim: false }, { position: null })), "/waivers?add=42#add-drop");
  assert.equal(eligibilityHref(person({ claim: "free_agent", claimable: true, needs_claim: false }, { espn_player_id: null })), null);
});

test("Sleeper free agents are never 'no claim needed'", () => {
  const c = claimText({ claim: "available_claim_unknown", claimable: true, needs_claim: null }, roster(), "Sleeper");
  assert.equal(c.text, "Unrostered");
  assert.match(c.detail, /Sleeper does not report waivers/);
  assert.doesNotMatch(c.detail, /without a claim/);
  assert.match(claimText({ claim: "free_agent", claimable: true, needs_claim: false }, roster(), "ESPN").detail, /without a claim/);
});

test("certainty labels never claim a confirmed change", () => {
  for (const label of Object.values(CERTAINTY_LABEL)) assert.doesNotMatch(label, /^confirmed|is confirmed/i);
});

test("sample, evidence and personnel copy", () => {
  assert.equal(sampleText(scenario()), "4 games with him, 2 without (weeks 5, 6)");
  assert.equal(sampleText(scenario({ sample: { team_games_with: 1, team_games_without: 0, weeks_without: [] } })), "1 game with him, 0 without");
  assert.equal(evidenceText({ source: "nfl_report", week: 7, report_status: null, practice_status: "Full Participation in Practice" }),
    "Week 7 NFL report: no game designation · Full Participation in Practice");
  assert.equal(evidenceText({ source: "provider_status", provider: "ESPN", status: "DAY_TO_DAY" }), "ESPN status: day to day");
  assert.equal(personnelText({ kind: "missed", week: 5, games: 2 }, "KC"), "Missed the last 2 games (since week 5)");
  assert.equal(personnelText({ kind: "returned", week: 6, games: 1 }, "KC"), "Back in week 6 after missing 1 game");
  assert.equal(personnelText({ kind: "first_game", week: 5, games: null }, "KC"), "First game for KC this season in week 5");
});

test("missing shares stay missing", () => {
  assert.equal(sharePct(null), null);
  assert.equal(sharePct(0), "0%");
  assert.equal(sharePct(0.456), "46%");
});

test("team summary counts observed, hypothesis and abstentions separately", () => {
  const t = {
    scenarios: [scenario({ effects: [effect("observed")] }), scenario({ kind: "teammate_returning", outcome: "abstain" }),
      scenario({ outcome: "hypothesis", effects: [effect("hypothesis"), effect("hypothesis")] })],
  } as TreeTeam;
  assert.deepEqual(teamSummary(t), { scenarios: "2 unavailable · 1 returning", outcome: "1 observed · 2 hypotheses · 1 abstention" });
  assert.equal(teamSummary({ scenarios: [scenario({ outcome: "no_supported_beneficiary" })] } as TreeTeam).outcome, "No supported beneficiary");
});

test("the claim link needs a safe integer player id", () => {
  const own: Ownership = { claim: "waivers", claimable: true, needs_claim: true };
  assert.equal(eligibilityHref(person(own, { espn_player_id: 1.5 })), null);
  assert.equal(eligibilityHref(person(own, { espn_player_id: Number.MAX_SAFE_INTEGER + 1 })), null);
  assert.equal(eligibilityHref(person(own, { espn_player_id: "42" as unknown as number })), null);
  assert.equal(eligibilityHref(person(own, { espn_player_id: -16002, position: "D/ST" })), "/waivers?add=-16002#add-drop");
});

test("'the volume spread out' never renders with fewer than 2 games without him", () => {
  for (const n of [0, 1]) {
    for (const outcome of ["no_supported_beneficiary", "thin_history"] as const) {
      for (const kind of ["teammate_unavailable", "teammate_returning"] as const) {
        const text = emptyEffectsText(scenario({ kind, outcome, sample: { team_games_with: 4, team_games_without: n, weeks_without: [] } }));
        assert.doesNotMatch(text, /spread out/, `${n} ${outcome} ${kind}`);
        assert.match(text, /too few games/);
      }
    }
  }
  assert.match(emptyEffectsText(scenario({ outcome: "no_supported_beneficiary" })), /Over 2 games without him .* spread out/);
  assert.equal(emptyEffectsText(scenario({ outcome: "thin_history", sample: { team_games_with: 5, team_games_without: 1, weeks_without: [6] } })),
    "Only 1 game without him: too few games to observe a change.");
});

test("an offline or unreadable answer never shows the backend address", () => {
  const offline = treeErrorText({ kind: "offline", error: "Cannot reach the backend at http://10.0.0.5:8000. Start it with: cd backend && uv run uvicorn app.main:app --reload" });
  assert.doesNotMatch(offline, /10\.0\.0\.5|http|uvicorn/);
  assert.doesNotMatch(treeErrorText({ kind: "unknown", error: "The backend at http://10.0.0.5:8000 returned a response that is not valid JSON." }), /http/);
  assert.equal(treeErrorText({ kind: "http", error: "League not found." }), "League not found.");
});

test("weekly-roster evidence is labelled weaker", () => {
  assert.equal(evidenceText({ source: "weekly_roster", week: 6, status: "ACT" }), "Week 6 weekly roster: ACT (weaker evidence than a report)");
  assert.equal(CERTAINTY_LABEL.roster_only, "Roster only, not confirmed");
});
