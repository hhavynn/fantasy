import assert from "node:assert/strict";
import { test } from "node:test";
import {
  buildActionItems, type ActionSources, type MatchupInput, type RosterInput, type StartSitInput, type WaiverInput,
} from "./action-items.ts";
import type { SignalProvenance } from "./signal-provenance.ts";
import {
  WEEKLY_DECISION_LIMIT, buildWeeklyHub, decisionHref, hubVerdict, prioritize, staleNote, suppressDuplicates,
  type DecisionContext, type HubSignals,
} from "./weekly-decisions.ts";

const LG = "lg_abcdefghijklmnop";
const CLASSIC: DecisionContext = { league: null, team: null, week: 6, projectionSource: "espn" };
const SCOPED: DecisionContext = { league: LG, team: "4", week: 6, projectionSource: "ours" };

const starter = (id: number, over: Partial<RosterInput> = {}): RosterInput => ({
  espn_player_id: id, full_name: `P${id}`, position: "WR", lineup_slot: "WR", is_starter: true, on_bye: false,
  availability: { status: "AVAILABLE", sources: [] }, ...over,
});
const matchup = (lineup: MatchupInput["me"]["lineup"] = [{ slot: "QB", player_id: 1 }]): MatchupInput => ({ status: "upcoming", me: { lineup } });
type Decision = StartSitInput["decisions"][number];
const swap = (id: string, gain: number | null, over: Partial<Decision> = {}): Decision => ({
  id, action: "SWAP", actionability: "ACTIONABLE", target_slot: "WR", projected_net_gain: gain,
  start_players: [{ id: 50, name: "Bench Guy", position: "WR" }], bench_players: [{ id: 2, name: "P2", position: "WR" }], ...over,
});
const startSit = (decisions: Decision[], status?: string): StartSitInput => ({ scoring_period: 6, summary: { starters_count: 9, status }, decisions });
const waiver = (id: number, position = "RB", over: Partial<WaiverInput> = {}): WaiverInput => ({
  espn_player_id: id, full_name: `W${id}`, position,
  components: { decision: { action: "ADD_NOW", actionable: true, roster_move: { summary: "Open spot.", drop: null }, utility: { forward: { total_gain: null, weeks: [] } } } },
  ...over,
});
const sources = (over: Partial<ActionSources> = {}): ActionSources => ({
  roster: [starter(1)], matchup: matchup(), startSit: startSit([]), waivers: { players: [], total: 0 }, ...over,
});

const sig = (signal: string, over: Partial<SignalProvenance> = {}): SignalProvenance => ({
  signal, label: `${signal} label`, source: "espn", captured_at: "2026-10-20T12:00:00Z", snapshot_version: `v-${signal}`,
  coverage: null, reason_unavailable: null, stale_reason: null, stale_after_hours: 36, ...over,
});
const SIGNALS: HubSignals = {
  overview: [sig("league", { snapshot_version: "ov-1" }), sig("injuries", { source: "nflverse" }), sig("usage", { source: "nflverse" })],
  startSit: [sig("league", { snapshot_version: "ss-1" }), sig("projections"), sig("injuries", { source: "nflverse" })],
  matchup: [sig("league", { snapshot_version: "m-1" }), sig("projections")],
  waivers: [sig("league", { snapshot_version: "w-1" }), sig("analysis", { source: "fantasy_intel" })],
};
const hubOf = (src: ActionSources, ctx = CLASSIC, signals = SIGNALS) => buildWeeklyHub(buildActionItems(src), ctx, signals);
const ids = (hub: ReturnType<typeof hubOf>) => [hub.primary?.id ?? null, ...hub.groups.flatMap((g) => g.decisions.map((d) => d.id))];

// ---------- priority ----------

test("actionable decisions come before informational ones, then locked; by urgency within each", () => {
  const ordered = prioritize([
    { id: "info", status: "informational", urgency: "low" },
    { id: "locked", status: "locked", urgency: "high" },
    { id: "swap", status: "actionable", urgency: "medium" },
    { id: "out", status: "actionable", urgency: "high" },
    { id: "unavailable", status: "unavailable", urgency: "low" },
    { id: "swap2", status: "actionable", urgency: "medium" },
  ] as const);
  assert.deepEqual(ordered.map((x) => x.id), ["out", "swap", "swap2", "info", "locked", "unavailable"]);
});

test("the primary next step is the first actionable decision and is not repeated in the groups", () => {
  const hub = hubOf(sources({
    roster: [starter(1, { availability: { status: "QUESTIONABLE", sources: [] } }), starter(3, { availability: { status: "OUT", sources: ["ESPN OUT"] } })],
    startSit: startSit([swap("a", 2.5)]),
    waivers: { players: [waiver(9)], total: 1 },
  }));
  assert.equal(hub.primary?.id, "out-3");
  assert.equal(hub.primary?.status, "actionable");
  // display order: the groups follow their first decision's priority (an actionable pickup outranks a questionable
  // starter, but the roll-up is shown with the lineup group it belongs to)
  assert.deepEqual(ids(hub), ["out-3", "swap-a", "questionable", "waiver-9"]);
  assert.ok(!hub.groups.some((g) => g.decisions.some((d) => d.id === hub.primary?.id)));
  assert.deepEqual(hub.groups.map((g) => [g.label, g.decisions.map((d) => d.id)]), [["Lineup", ["swap-a", "questionable"]], ["Waivers", ["waiver-9"]]]);
});

test("an empty slot outranks a swap: it costs a whole slot", () => {
  const hub = hubOf(sources({ matchup: matchup([{ slot: "TE", player_id: null }]), startSit: startSit([swap("a", 4)]) }));
  assert.deepEqual(ids(hub), ["empty-TE", "swap-a"]);
});

test("at most five decisions, the primary included; the overflow is counted by destination", () => {
  const roster = [1, 2, 3, 4, 5, 6].map((id) => starter(id, { availability: { status: "OUT", sources: [] } }));
  const hub = hubOf(sources({ roster, waivers: { players: [waiver(9), waiver(10)], total: 4 } }));
  const shown = ids(hub);
  assert.equal(shown.length, WEEKLY_DECISION_LIMIT);
  assert.deepEqual(shown, ["out-1", "out-2", "out-3", "out-4", "out-5"]);
  assert.deepEqual(hub.more.map((m) => [m.key, m.count, m.label, m.href]), [
    ["lineup", 1, "1 more lineup decision", "/start-sit?week=6"],
    ["waivers", 4, "4 more waiver pickups", "/waivers"],
  ]);
});

// ---------- locked and unavailable ----------

test("locked and unverifiable moves are kept apart, with the engine's reason, and never become the next step", () => {
  const hub = hubOf(sources({ startSit: startSit([
    swap("locked", 6, { actionability: "LOCKED", warnings: ["P2 (KC) has already kicked off and cannot be moved."] }),
    swap("ir", 5, { actionability: "IR_BLOCKED", warnings: [] }),
    swap("uncertain", 4, { actionability: "UNCERTAIN", warnings: ["Kickoff status for P2 (KC) could not be verified."] }),
    swap("none", 0, { action: "NO_CHANGE", actionability: "NO_CHANGE" }),
  ]) }));
  assert.equal(hub.primary, null);
  const all = hub.groups.flatMap((g) => g.decisions);
  assert.deepEqual(all.map((d) => [d.id, d.status, d.statusReason]), [
    ["swap-locked", "locked", "P2 (KC) has already kicked off and cannot be moved."],
    ["swap-ir", "unavailable", "A player in this move is in an IR slot."],
    ["swap-uncertain", "unavailable", "Kickoff status for P2 (KC) could not be verified."],
  ]);
});

test("a pickup whose suggested drop left the roster cannot be verified and is not counted as actionable", () => {
  const stale = waiver(7, "WR", { drop_on_roster: false });
  stale.components.decision!.roster_move.drop = { name: "Gone Guy" };
  const list = buildActionItems(sources({ waivers: { players: [stale, waiver(8)], total: 2 } }));
  assert.deepEqual(list.items.filter((i) => i.category === "waivers").map((i) => i.id), ["waiver-8"]);
  assert.deepEqual(list.blocked.map((i) => [i.id, i.status]), [["waiver-7", "unavailable"]]);
  assert.match(list.blocked[0].statusReason ?? "", /no longer on the roster/);
  assert.equal(list.more.waivers, 0);
});

test("a failed source is named; nothing becomes an all-clear on partial knowledge", () => {
  const hub = hubOf({ roster: null, matchup: null, startSit: null, waivers: { players: [], total: 0 } });
  assert.equal(hub.primary, null);
  assert.deepEqual(hub.unavailable, ["Roster", "Matchup", "Start/Sit"]);
});

// ---------- evidence ----------

test("each decision carries its own source's provenance and snapshot; none is invented", () => {
  const hub = hubOf(sources({
    roster: [starter(3, { availability: { status: "OUT", sources: [] } }), starter(4, { market_signal: "SELL" })],
    startSit: startSit([swap("a", 2)]), waivers: { players: [waiver(9)], total: 1 },
  }));
  const by = Object.fromEntries([hub.primary!, ...hub.groups.flatMap((g) => g.decisions)].map((d) => [d.id, d.evidence]));
  assert.deepEqual(by["out-3"].signals.map((s) => s.signal), ["league", "injuries"]);
  assert.equal(by["out-3"].snapshotVersion, "ov-1");
  assert.deepEqual(by["swap-a"].signals.map((s) => s.signal), ["league", "projections", "injuries"]);
  assert.equal(by["swap-a"].snapshotVersion, "ss-1");
  assert.deepEqual(by["waiver-9"].signals.map((s) => s.signal), ["league", "analysis"]);
  assert.deepEqual(by["sell-signal"].signals.map((s) => s.signal), ["league", "usage"]);
  assert.equal(by["sell-signal"].source, "Overview");
  // an older backend without provenance: the evidence is empty (shown as unknown age), not made up
  const bare = hubOf(sources({ startSit: startSit([swap("a", 2)]) }), CLASSIC, {});
  assert.deepEqual(bare.primary?.evidence, { source: "Start/Sit", signals: [], snapshotVersion: null, status: null, stale: false });
});

test("sell signals are one informational trade item while trades are open, and none after the deadline", () => {
  const now = Date.parse("2026-10-22T12:00:00Z");
  const roster = [starter(4, { market_signal: "SELL" }), starter(5, { market_signal: "SELL", is_starter: false, lineup_slot: "BE" }), starter(6, { market_signal: "BUY" })];
  const open = buildActionItems(sources({ roster, league: { trade_deadline: "2026-11-30T17:00:00" }, now }));
  const sell = open.items.find((i) => i.id === "sell-signal")!;
  assert.equal(sell.status, "informational");
  assert.equal(sell.title, "2 players have a sell signal");
  assert.equal(open.items.some((i) => i.id === "trade-deadline"), false);   // more than a week away
  const one = buildActionItems(sources({ roster: [roster[1]], now }));
  assert.equal(one.items.find((i) => i.id === "sell-signal")?.reason.startsWith("WR, on the bench."), true);
  const soon = buildActionItems(sources({ roster, league: { trade_deadline: "2026-10-25 17:00:00" }, now }));
  assert.equal(soon.items.find((i) => i.id === "trade-deadline")?.title, "The trade deadline is in 3 days");
  const closed = buildActionItems(sources({ roster, league: { trade_deadline: "2026-10-21T17:00:00Z" }, now }));
  assert.equal(closed.items.some((i) => i.category === "trade" || i.category === "league"), false);
});

// ---------- duplicates ----------

test("a starter who is out and benched by a swap is one decision, the swap, carrying the reason", () => {
  const hub = hubOf(sources({
    roster: [starter(2, { availability: { status: "OUT", sources: [] } })],
    startSit: startSit([swap("a", 3.2)]),
  }));
  assert.deepEqual(ids(hub), ["swap-a"]);
  assert.equal(hub.primary?.title, "Start Bench Guy over P2");
  assert.equal(hub.primary?.reason, "P2 is out. +3.2 projected points at WR.");
  assert.equal(hub.primary?.urgency, "high");
  assert.equal(hub.primary?.tone, "negative");
});

test("a bye starter benched by a swap folds in the same way; others stay", () => {
  const hub = hubOf(sources({ roster: [starter(2, { on_bye: true }), starter(3, { on_bye: true })], startSit: startSit([swap("a", 1)]) }));
  assert.deepEqual(ids(hub), ["bye-3", "swap-a"]);
  assert.equal(hub.groups[0].decisions[0].reason, "P2 is on bye. +1.0 projected points at WR.");
});

test("questionable starters a swap benches leave the roll-up, which is dropped when none is left", () => {
  const q = { status: "QUESTIONABLE", sources: [] };
  const partly = hubOf(sources({ roster: [starter(2, { availability: q }), starter(3, { availability: q, lineup_slot: "TE" })], startSit: startSit([swap("a", 1)]) }));
  const roll = partly.groups.flatMap((g) => g.decisions).find((d) => d.id === "questionable")!;
  assert.equal(roll.title, "P3 is questionable");
  assert.equal(roll.reason, "Starting at TE.");
  const all = hubOf(sources({ roster: [starter(2, { availability: q })], startSit: startSit([swap("a", 1)]) }));
  assert.deepEqual(ids(all), ["swap-a"]);
});

test("an empty slot with an actionable pickup at that position is one decision, the pickup", () => {
  const hub = hubOf(sources({
    matchup: matchup([{ slot: "K", player_id: null }, { slot: "TE", player_id: null }]),
    waivers: { players: [waiver(9, "K")], total: 1 },
  }));
  assert.deepEqual(ids(hub), ["empty-TE", "waiver-9"]);
  const pickup = hub.groups.flatMap((g) => g.decisions).find((d) => d.id === "waiver-9")!;
  assert.equal(pickup.reason, "Your K slot is empty. Open spot.");
  assert.equal(pickup.urgency, "high");
});

test("suppressDuplicates keeps the first of two items with the same id", () => {
  const list = buildActionItems(sources({ startSit: startSit([swap("a", 2)]) }));
  const twice = suppressDuplicates([...list.items, ...list.items]);
  assert.deepEqual(twice.map((i) => i.id), ["swap-a"]);
});

// ---------- scoped links ----------

test("links keep the week on week pages, the team on team pages and the league prefix everywhere in-app", () => {
  assert.equal(decisionHref("/start-sit", CLASSIC), "/start-sit?week=6");
  assert.equal(decisionHref("/start-sit", SCOPED), `/l/${LG}/start-sit?week=6&team=4`);
  assert.equal(decisionHref("/matchup", SCOPED), `/l/${LG}/matchup?week=6&team=4`);
  assert.equal(decisionHref("/waivers?view=STREAMING&pos=D%2FST", SCOPED), `/l/${LG}/waivers?view=STREAMING&pos=D%2FST&team=4`);
  assert.equal(decisionHref("/trades/discover", SCOPED), `/l/${LG}/trades/discover?team=4`);
  // not team-scoped: the league only
  assert.equal(decisionHref("/managers/3", SCOPED), `/l/${LG}/managers/3`);
  assert.equal(decisionHref("/rankings", SCOPED), `/l/${LG}/rankings`);
  // an href that names its own week or team keeps it
  assert.equal(decisionHref("/start-sit?week=7", SCOPED), `/l/${LG}/start-sit?week=7&team=4`);
  assert.equal(decisionHref("/start-sit?team=2", SCOPED), `/l/${LG}/start-sit?team=2&week=6`);
  // no week known: none is added
  assert.equal(decisionHref("/start-sit", { ...CLASSIC, week: null }), "/start-sit");
});

test("every decision link of a scoped hub lands in its league, team and week; the projection source rides along in context", () => {
  const hub = hubOf(sources({
    roster: [starter(3, { availability: { status: "OUT", sources: [] } }), starter(4, { market_signal: "SELL" })],
    matchup: matchup([{ slot: "D/ST", player_id: null }]),
    startSit: startSit([swap("a", 2), swap("b", 1, { id: "b", actionability: "LOCKED" })]),
    waivers: { players: [waiver(9)], total: 3 },
  }), SCOPED);
  const all = [hub.primary!, ...hub.groups.flatMap((g) => g.decisions)];
  assert.equal(all.length, WEEKLY_DECISION_LIMIT);
  for (const d of all) {
    assert.ok(d.href.startsWith(`/l/${LG}/`), d.href);
    assert.match(d.href, /[?&]team=4(&|$)/, d.href);
    if (/\/(start-sit|matchup)\?/.test(d.href)) assert.match(d.href, /[?&]week=6(&|$)/, d.href);
    // the source is the visitor's switch (a cookie the destination reads), never a param that could contradict it
    assert.doesNotMatch(d.href, /[?&]source=/, d.href);
    assert.deepEqual(d.context, SCOPED);
  }
  assert.ok(all.some((d) => d.href === `/l/${LG}/waivers?view=STREAMING&pos=D%2FST&team=4`));
  for (const m of hub.more) assert.ok(m.href.startsWith(`/l/${LG}/`) && m.href.includes("team=4"), m.href);
});

test("the classic site with the owner's default team adds neither a league prefix nor a team", () => {
  const hub = hubOf(sources({ startSit: startSit([swap("a", 2)]), waivers: { players: [waiver(9)], total: 1 } }));
  assert.deepEqual([hub.primary!, ...hub.groups.flatMap((g) => g.decisions)].map((d) => d.href), ["/start-sit?week=6", "/waivers"]);
});

test("a week-based (Sleeper) deadline closes trades only once the league is past that week, and is noted this week and next", () => {
  const roster = [starter(4, { market_signal: "SELL" })];
  const at = (current: number, deadlineWeek: number | null) =>
    buildActionItems(sources({ roster, provider: "sleeper", league: { trade_deadline: null, trade_deadline_week: deadlineWeek, current_week: current } }));
  const kinds = (l: ReturnType<typeof at>) => l.items.filter((i) => i.category === "trade" || i.category === "league").map((i) => i.id);
  assert.deepEqual(kinds(at(12, 11)), []);                                  // past the deadline week: no sell signal, no notice
  assert.deepEqual(kinds(at(11, 11)), ["sell-signal", "trade-deadline"]);   // the deadline week itself: still open
  assert.equal(at(11, 11).items.find((i) => i.id === "trade-deadline")?.title, "The trade deadline is this week (week 11)");
  assert.equal(at(10, 11).items.find((i) => i.id === "trade-deadline")?.title, "The trade deadline is next week (week 11)");
  assert.deepEqual(kinds(at(8, 11)), ["sell-signal"]);                      // further off: no notice
  assert.deepEqual(kinds(at(12, null)), ["sell-signal"]);                   // unknown stays unknown: not closed, no notice
  const noWeek = buildActionItems(sources({ roster, league: { trade_deadline: null, trade_deadline_week: 11, current_week: null } }));
  assert.deepEqual(kinds(noWeek), ["sell-signal"]);
  // a timestamp deadline (ESPN) wins over a week number
  const both = buildActionItems(sources({ roster, now: Date.parse("2026-10-22T12:00:00Z"),
    league: { trade_deadline: "2026-12-01T00:00:00Z", trade_deadline_week: 3, current_week: 8 } }));
  assert.deepEqual(kinds(both), ["sell-signal"]);
});

// ---------- #165 closure audit: the four proven failing cases ----------

// Every signal of the fixtures was captured at 2026-10-20T12:00Z with a 36h window: at this time all are current.
const FRESH = Date.parse("2026-10-21T00:00:00Z");
const hubAt = (src: ActionSources, signals = SIGNALS, now = FRESH) => buildWeeklyHub(buildActionItems(src), CLASSIC, signals, undefined, now);

test("an out starter whose only swap is locked is one locked decision, never the actionable next step", () => {
  const hub = hubAt(sources({
    roster: [starter(2, { availability: { status: "OUT", sources: ["ESPN"] } })],
    startSit: startSit([swap("a", 6, { actionability: "LOCKED", warnings: ["Bench Guy (KC) has already kicked off and cannot be moved."] })]),
  }));
  assert.equal(hub.primary, null);
  const all = hub.groups.flatMap((g) => g.decisions);
  assert.deepEqual(all.map((d) => [d.id, d.status]), [["swap-a", "locked"]]);
  assert.match(all[0].reason, /^P2 is out\. /);
  assert.equal(all[0].statusReason, "Bench Guy (KC) has already kicked off and cannot be moved.");
  // an actionable swap for him still wins over a locked one, and an unverifiable one folds the same way
  const both = hubAt(sources({
    roster: [starter(2, { availability: { status: "OUT", sources: [] } })],
    startSit: startSit([swap("locked", 6, { actionability: "LOCKED" }), swap("open", 3)]),
  }));
  assert.equal(both.primary?.id, "swap-open");
  assert.match(both.primary!.reason, /^P2 is out\. /);
  // a locked fold keeps the swap's caution tone: it is not something to do
  assert.equal(all[0].tone, "caution");
  // an unverifiable or IR-blocked swap does not absorb him: benching a starter who is out stays the next step
  for (const actionability of ["UNCERTAIN", "IR_BLOCKED"]) {
    const unverified = hubAt(sources({
      roster: [starter(2, { on_bye: true })], startSit: startSit([swap("u", 4, { actionability })]),
    }));
    assert.equal(unverified.primary?.id, "bye-2");
    assert.deepEqual(unverified.groups.flatMap((g) => g.decisions).map((d) => [d.id, d.status]), [["swap-u", "unavailable"]]);
  }
});

test("Start/Sit all_locked: no lineup alert is the actionable next step; non-lineup decisions are untouched", () => {
  const hub = hubAt(sources({
    roster: [starter(2, { availability: { status: "OUT", sources: [] } }), starter(3, { availability: { status: "QUESTIONABLE", sources: [] } }), starter(4, { market_signal: "SELL" })],
    matchup: matchup([{ slot: "RB", player_id: null }]),
    startSit: startSit([], "all_locked"),
    waivers: { players: [waiver(9, "RB")], total: 1 },
  }));
  const all = [hub.primary!, ...hub.groups.flatMap((g) => g.decisions)];
  const status = Object.fromEntries(all.map((d) => [d.id, d.status]));
  assert.deepEqual(status, { "out-2": "locked", questionable: "locked", "empty-RB": "locked", "waiver-9": "actionable", "sell-signal": "informational" });
  // the pickup stays a roster move, but no longer claims to fill a slot that cannot be changed
  assert.equal(hub.primary?.id, "waiver-9");
  assert.doesNotMatch(hub.primary!.reason, /slot is empty/);
  for (const d of all.filter((x) => x.status === "locked")) assert.match(d.statusReason ?? "", /Every game on the roster has kicked off/);
  // nothing else on the roster: no actionable next step at all
  const only = hubAt(sources({ roster: [starter(2, { availability: { status: "OUT", sources: [] } })], startSit: startSit([], "all_locked") }));
  assert.equal(only.primary, null);
  // the engine reports incomplete_data before all_locked: the lock counts still lock the lineup alerts
  const out = [starter(2, { availability: { status: "OUT", sources: [] } })];
  const counted = (over: Partial<StartSitInput["summary"]>) => hubAt(sources({ roster: out, startSit: {
    ...startSit([], "incomplete_data"), summary: { starters_count: 9, status: "incomplete_data", bench_count: 6, ir_count: 1, ...over } } }));
  const allLocked = counted({ locked_players_count: 16 });
  assert.equal(allLocked.primary, null);
  assert.deepEqual(allLocked.partial.map((p) => p.source), ["Start/Sit"]);
  assert.equal(counted({ locked_players_count: 15 }).primary?.id, "out-2");
  // held starters (lineup_coverage) are locked but in no count: no lock is inferred from the counts then
  assert.equal(counted({ locked_players_count: 16, lineup_coverage: { status: "partial" } }).primary?.id, "out-2");
  // an older backend without the counts infers nothing
  assert.equal(counted({}).primary?.id, "out-2");
});

test("Start/Sit incomplete_data is never \"Lineup is set\": the hub is partial and says why", () => {
  const hub = hubAt(sources({ startSit: startSit([], "incomplete_data") }));
  assert.equal(hub.primary, null);
  assert.deepEqual(hub.partial.map((p) => p.source), ["Start/Sit"]);
  const v = hubVerdict(hub)!;
  assert.notEqual(v.headline, "Lineup is set");
  assert.equal(v.tone, "caution");
  assert.match(v.body, /no projection.*not an all-clear/);
  // the same week with complete data is the all-clear, and a failed source still outranks a partial one
  assert.equal(hubVerdict(hubAt(sources({ startSit: startSit([], "optimal") })))?.headline, "Lineup is set");
  const both = hubVerdict(hubAt(sources({ startSit: startSit([], "incomplete_data"), waivers: null })))!;
  assert.equal(both.headline, "Nothing to act on in what loaded");
  assert.match(both.body, /Couldn't check Waivers.*no projection/);
});

test("stale evidence never holds the next step over current evidence, and says so when nothing current is left", () => {
  const superseded: HubSignals = { ...SIGNALS, startSit: SIGNALS.startSit!.map((x) => (x.signal === "league" ? { ...x, stale_reason: "A newer sync failed." } : x)) };
  const src = sources({ startSit: startSit([swap("a", 6)]), waivers: { players: [waiver(9)], total: 1 } });
  const hub = hubAt(src, superseded);
  assert.equal(hub.primary?.id, "waiver-9");
  const swapD = hub.groups.flatMap((g) => g.decisions).find((d) => d.id === "swap-a")!;
  assert.equal(swapD.evidence.status, "stale");
  assert.equal(staleNote(hub.primary!), null);
  // with current evidence the swap keeps its place
  assert.equal(hubAt(src).primary?.id, "swap-a");
  // only stale evidence: the first actionable decision stays, and its note says the evidence is stale
  const alone = hubAt(sources({ startSit: startSit([swap("a", 6)]) }), superseded);
  assert.equal(alone.primary?.id, "swap-a");
  assert.match(staleNote(alone.primary!) ?? "", /Start\/Sit data behind this is stale/);
  // past the refresh window counts as stale too; unknown age (no capture time) is not demoted
  const later = Date.parse("2026-10-23T12:00:00Z");
  assert.equal(hubAt(src, SIGNALS, later).primary?.evidence.status, "stale");
  const unknown: HubSignals = { ...SIGNALS, startSit: SIGNALS.startSit!.map((x) => ({ ...x, captured_at: null })) };
  assert.equal(hubAt(src, unknown).primary?.id, "swap-a");
  // a superseded signal still demotes when another signal of the same payload is of unknown age (a worse status)
  const mixed: HubSignals = { ...SIGNALS, startSit: superseded.startSit!.map((x) => (x.signal === "projections" ? { ...x, captured_at: null } : x)) };
  const m = hubAt(src, mixed);
  assert.equal(m.primary?.id, "waiver-9");
  const mixedSwap = m.groups.flatMap((g) => g.decisions).find((d) => d.id === "swap-a")!;
  assert.deepEqual([mixedSwap.evidence.status, mixedSwap.evidence.stale], ["unknown", true]);
});
