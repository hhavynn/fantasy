import assert from "node:assert/strict";
import { test } from "node:test";
import {
  buildActionItems, type ActionSources, type MatchupInput, type RosterInput, type StartSitInput, type WaiverInput,
} from "./action-items.ts";

const starter = (id: number, over: Partial<RosterInput> = {}): RosterInput => ({
  espn_player_id: id, full_name: `P${id}`, position: "WR", lineup_slot: "WR", is_starter: true, on_bye: false,
  availability: { status: "AVAILABLE", sources: [] }, ...over,
});

const matchup = (over: Partial<MatchupInput> = {}): MatchupInput => ({
  status: "upcoming", me: { lineup: [{ slot: "QB", player_id: 1 }, { slot: "K", player_id: 2 }] }, ...over,
});

const startSit = (decisions: StartSitInput["decisions"], starters = 8): StartSitInput => ({
  scoring_period: 6, summary: { starters_count: starters }, decisions,
});

const swap = (id: string, gain: number | null, over: Partial<StartSitInput["decisions"][number]> = {}): StartSitInput["decisions"][number] => ({
  id, action: "SWAP", actionability: "ACTIONABLE", target_slot: "FLEX", projected_net_gain: gain,
  start_players: [{ id: 1, name: "Bench Guy", position: "WR" }], bench_players: [{ id: 2, name: "Starter Guy", position: "WR" }], ...over,
});

const waiver = (id: number, over: Partial<NonNullable<WaiverInput["components"]["decision"]>> = {}): WaiverInput => ({
  espn_player_id: id, full_name: `W${id}`, position: "RB",
  components: { decision: { action: "ADD_NOW", actionable: true, roster_move: { summary: "Open spot.", drop: null },
    utility: { forward: { total_gain: null, weeks: [] } }, ...over } },
});

const waivers = (players: WaiverInput[], total = players.length) => ({ players, total });

const clean: ActionSources = { roster: [starter(1)], matchup: matchup(), startSit: startSit([{ ...swap("x", 1), action: "NO_CHANGE", actionability: "NO_CHANGE" }]), waivers: waivers([]) };

test("nothing actionable gives an empty list with every source available", () => {
  const r = buildActionItems(clean);
  assert.deepEqual(r.items, []);
  assert.deepEqual(r.unavailable, []);
  assert.deepEqual(r.more, { swaps: 0, waivers: 0 });
});

test("only ACTIONABLE start/sit moves are listed; locked, uncertain, IR-blocked and no-change are not", () => {
  const r = buildActionItems({ ...clean, startSit: startSit([
    swap("a", 3.2), swap("b", 5, { actionability: "LOCKED" }), swap("c", 5, { actionability: "UNCERTAIN" }),
    swap("d", 5, { actionability: "IR_BLOCKED" }), swap("e", 0, { action: "NO_CHANGE", actionability: "NO_CHANGE" }),
  ]) });
  assert.deepEqual(r.items.map((i) => i.id), ["swap-a"]);
  assert.equal(r.items[0].title, "Start Bench Guy over Starter Guy");
  assert.equal(r.items[0].reason, "+3.2 projected points at FLEX.");
  assert.equal(r.items[0].href, "/start-sit");
});

test("swaps are ordered by projected gain, capped, and the overflow is counted", () => {
  const r = buildActionItems({ ...clean, startSit: startSit([swap("a", 1), swap("b", 4), swap("c", 2), swap("d", 3)]) });
  assert.deepEqual(r.items.map((i) => i.id), ["swap-b", "swap-d", "swap-c"]);
  assert.equal(r.more.swaps, 1);
});

test("a swap with no gain number states none; a non-positive gain is not an improvement", () => {
  const r = buildActionItems({ ...clean, startSit: startSit([swap("a", null), swap("b", 0), swap("c", -1)]) });
  assert.deepEqual(r.items.map((i) => i.id), ["swap-a"]);
  assert.doesNotMatch(r.items[0].reason, /\d/);
});

test("without a submitted lineup there is no gain to show: one note, no swaps", () => {
  const r = buildActionItems({ ...clean, startSit: startSit([swap("a", null)], 0) });
  assert.deepEqual(r.items.map((i) => i.id), ["no-lineup"]);
  assert.equal(r.items[0].title, "No lineup submitted for week 6");
});

test("coordinated swaps name every player", () => {
  const d = swap("a", 2, {
    action: "COORDINATED_SWAP",
    start_players: [{ id: 1, name: "A", position: "WR" }, { id: 2, name: "B", position: "RB" }, { id: 3, name: "C", position: "TE" }],
    bench_players: [{ id: 4, name: "D", position: "WR" }],
  });
  assert.equal(buildActionItems({ ...clean, startSit: startSit([d]) }).items[0].title, "Start A, B and C over D");
});

test("starters who are out, doubtful, on IR, suspended or on bye are listed first; benched ones are not", () => {
  const roster = [
    starter(1, { availability: { status: "OUT", sources: ["ESPN OUT"] } }),
    starter(2, { on_bye: true }),
    starter(3, { availability: { status: "DOUBTFUL", sources: ["NFL report Doubtful"] } }),
    starter(4, { availability: { status: "OUT", sources: [] }, is_starter: false }),
  ];
  const r = buildActionItems({ ...clean, roster, startSit: startSit([swap("a", 2)]) });
  assert.deepEqual(r.items.map((i) => i.id), ["out-1", "bye-2", "out-3", "swap-a"]);
  assert.equal(r.items[0].title, "P1 is out");
  assert.equal(r.items[0].reason, "Starting at WR · ESPN OUT.");
  assert.equal(r.items[1].title, "P2 is on bye");
  assert.ok(r.items.slice(0, 3).every((i) => i.tone === "negative"));
});

test("questionable starters roll up into one item", () => {
  const q = { status: "QUESTIONABLE", sources: ["ESPN QUESTIONABLE"] };
  const one = buildActionItems({ ...clean, roster: [starter(1, { availability: q })] });
  assert.equal(one.items[0].title, "P1 is questionable");
  const many = buildActionItems({ ...clean, roster: [starter(1, { availability: q }), starter(2, { availability: q }), starter(3, { availability: q })] });
  assert.equal(many.items.length, 1);
  assert.equal(many.items[0].title, "3 starters are questionable");
  assert.equal(many.items[0].reason, "P1, P2 and P3.");
});

test("an empty slot in the best lineup points at the matching pickups", () => {
  const m = matchup({ me: { lineup: [{ slot: "QB", player_id: 1 }, { slot: "D/ST", player_id: null }, { slot: "TE", player_id: null }, { slot: "FLEX", player_id: null }] } });
  const r = buildActionItems({ ...clean, matchup: m });
  assert.deepEqual(r.items.map((i) => i.title), ["Empty D/ST slot", "Empty TE slot", "Empty FLEX slot"]);
  assert.equal(r.items[0].href, "/waivers?view=STREAMING&pos=D%2FST");
  assert.equal(r.items[1].href, "/waivers?pos=TE");
  assert.equal(r.items[2].href, "/waivers");
});

test("a finished week raises no lineup alerts", () => {
  const roster = [starter(1, { availability: { status: "OUT", sources: [] } })];
  const m = matchup({ status: "final", me: { lineup: [{ slot: "K", player_id: null }] } });
  assert.deepEqual(buildActionItems({ ...clean, roster, matchup: m }).items, []);
});

test("waiver pickups: only actionable, capped, with the drop and the forward gain", () => {
  const w = [
    waiver(1, { roster_move: { summary: "s", drop: { name: "Dropper" } }, utility: { forward: { total_gain: 12.34, weeks: [1, 2, 3] } } }),
    waiver(2, { action: "WAIVER_CLAIM" }),
    waiver(3),
    waiver(4, { actionable: false, action: "MONITOR" }),
  ];
  const r = buildActionItems({ ...clean, waivers: waivers(w, 7) });
  assert.deepEqual(r.items.map((i) => i.id), ["waiver-1", "waiver-2"]);
  assert.equal(r.items[0].title, "Add W1 (RB)");
  assert.equal(r.items[0].reason, "drop Dropper · +12.3 projected lineup points over 3 weeks.");
  assert.equal(r.items[1].title, "Claim W2 (RB)");
  assert.equal(r.items[1].reason, "Open spot.");
  // the page fetches only a few rows; the overflow comes from the endpoint's total, not the rows in hand
  assert.equal(r.more.waivers, 5);
  assert.equal(buildActionItems({ ...clean, waivers: waivers(w.slice(0, 2), 2) }).more.waivers, 0);
  assert.equal(buildActionItems({ ...clean, waivers: waivers(w.slice(0, 3), 40) }).more.waivers, 38);
});

test("a failed source is named and does not hide the others", () => {
  const r = buildActionItems({ roster: null, matchup: matchup(), startSit: null, waivers: waivers([waiver(1)]) });
  assert.deepEqual(r.unavailable, ["Roster", "Start/Sit"]);
  assert.deepEqual(r.items.map((i) => i.id), ["waiver-1"]);
  const all = buildActionItems({ roster: null, matchup: null, startSit: null, waivers: null });
  assert.deepEqual(all.items, []);
  assert.equal(all.unavailable.length, 4);
});

test("priority order: lineup problems, then swaps, empty slots, questionable, pickups", () => {
  const q = { status: "QUESTIONABLE", sources: [] };
  const r = buildActionItems({
    roster: [starter(1, { availability: q }), starter(2, { availability: { status: "OUT", sources: [] } })],
    matchup: matchup({ me: { lineup: [{ slot: "K", player_id: null }] } }),
    startSit: startSit([swap("a", 2)]),
    waivers: waivers([waiver(9)]),
  });
  assert.deepEqual(r.items.map((i) => i.id), ["out-2", "swap-a", "empty-K", "questionable", "waiver-9"]);
});
