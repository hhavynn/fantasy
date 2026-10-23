import assert from "node:assert/strict";
import { test } from "node:test";
import {
  ACTION_CENTER_CONTRACT, CARD_DECISIONS, NO_PROJECTIONS, applyLocks, buildActionCenter, buildCard, centerProblem, kickoffText,
  parseActionCenter, projectionSourceOf, projectionsNote, rankCards, sectionContext, untilText, type CenterCard,
} from "./action-center.ts";
import { buildActionItems, type RosterInput } from "./action-items.ts";

const LG = (n: number) => `lg_${"abcdefghijklmnop"}${n}`;
const T0 = Date.parse("2026-10-25T12:00:00Z");
const iso = (hours: number) => new Date(T0 + hours * 36e5).toISOString();

const league = (n: number, over: Record<string, unknown> = {}) => ({
  league_id: LG(n), provider: "espn", external_league_id: String(1000 + n), season: 2026, name: `League ${n}`,
  visibility: "private", status: "active", role: "viewer", pinned: false, perspective_team_id: "3",
  sync_state: "ready", sync_issue: null, data: { data_version: "v1", global_version: "g1", committed_at: "2026-10-25T10:00:00Z" },
  ...over,
});
const starter = (id: number, over: Partial<RosterInput> = {}): RosterInput => ({
  espn_player_id: id, full_name: `P${id}`, position: "WR", lineup_slot: "WR", is_starter: true, on_bye: false,
  availability: { status: "AVAILABLE", sources: [] }, ...over,
});
const sig = { signal: "league", label: "League", source: "espn", captured_at: "2026-10-25T10:00:00Z", snapshot_version: "v1",
  coverage: null, reason_unavailable: null, stale_reason: null, stale_after_hours: 36 };
const prov = { contract: "signal-provenance.v1", signals: [sig] };

function ready(n: number, { roster = [starter(1)], decisions = [] as unknown[], players = {} as Record<string, unknown>, next = null as string | null,
  stale = false, startSit = true } = {}) {
  return {
    league: league(n), state: "ready", reason: null, stale, degraded: stale ? "slow" : null,
    versions: { data_version: "v1", global_version: "g1" }, team: { team_id: 3, name: `Team ${n}` }, week: 7,
    overview: { league: {}, team: { team_id: 3 }, roster, provenance: prov },
    start_sit: startSit ? { scoring_period: 7, summary: { starters_count: 9, status: "optimal" }, decisions, projection_source: "ESPN", provenance: prov } : null,
    lock: { next_kickoff_utc: next, starters: { unlocked: 1, locked: 0, unknown: 0, no_game: 0 }, players, rule: { value: "individual_game", display: "Each player locks at his own game's kickoff", reason: null } },
  };
}
const notReady = (n: number, state: string, reason: string, over: Record<string, unknown> = {}) => ({
  league: league(n, over), state, reason, stale: false, versions: null, team: null, week: null, overview: null, start_sit: null, lock: null,
});
const payload = (leagues: unknown[], more: unknown[] = []) => ({ contract: ACTION_CENTER_CONTRACT, limit: 8, generated_at: "2026-10-25T12:00:00+00:00", leagues, more: { count: more.length, leagues: more } });
const parse = (p: unknown) => {
  const out = parseActionCenter(p);
  assert.ok(out);
  return out;
};

test("an unknown contract or shape is not read", () => {
  assert.equal(parseActionCenter(null), null);
  assert.equal(parseActionCenter({ contract: "other", leagues: [] }), null);
  assert.deepEqual(parse(payload([{ nope: 1 }])).leagues, []);
});

test("a section without an explicit fresh flag is not fresh, and only ready sections carry data", () => {
  const raw = ready(1) as Record<string, unknown>;
  delete raw.stale;
  assert.equal(parse(payload([raw])).leagues[0].stale, true);
  const sneaky = { ...notReady(2, "not_ready", "No data."), overview: { roster: [starter(1)] } };
  assert.equal(parse(payload([sneaky])).leagues[0].overview, null);
});

test("an out starter is the next step, deep-linked to the league, team and week", () => {
  const s = parse(payload([ready(1, { roster: [starter(5, { full_name: "Out Guy", availability: { status: "OUT", sources: ["ESPN"] } })],
    players: { 5: { lock_status: "UNLOCKED", kickoff_utc: iso(5) } }, next: iso(2) })])).leagues[0];
  const card = buildCard(s);
  assert.equal(card.decisions[0].decision.title, "Out Guy is out");
  assert.equal(card.decisions[0].decision.href, `/l/${LG(1)}/start-sit?week=7&team=3`);
  assert.equal(card.deadline, T0 + 5 * 36e5);             // his own kickoff, not the league's first
  assert.equal(card.href, `/l/${LG(1)}?team=3`);
  assert.equal(card.signals.length, 1);
  assert.deepEqual(card.unavailable, []);                  // matchup and waivers are not read here: not "missing"
});

test("a starter whose game kicked off is locked, not something to do", () => {
  const roster = [starter(5, { full_name: "Out Guy", availability: { status: "OUT", sources: [] } }),
    starter(6, { full_name: "Q1", availability: { status: "QUESTIONABLE", sources: [] } }),
    starter(7, { full_name: "Q2", availability: { status: "QUESTIONABLE", sources: [] } })];
  const players = { 5: { lock_status: "LOCKED", kickoff_utc: iso(-1) }, 6: { lock_status: "LOCKED", kickoff_utc: iso(-1) }, 7: { lock_status: "UNLOCKED", kickoff_utc: iso(3) } };
  const list = applyLocks(buildActionItems({ roster, startSit: null, matchup: null, waivers: null }), players);
  assert.equal(list.items.find((i) => i.id === "out-5"), undefined);
  assert.equal(list.blocked.find((i) => i.id === "out-5")?.status, "locked");
  assert.deepEqual(list.items.find((i) => i.id === "questionable")?.players.map((p) => p.name), ["Q2"]);
  const card = buildCard(parse(payload([ready(1, { roster, players })])).leagues[0]);
  assert.ok(card.decisions.every((x) => x.decision.status !== "actionable" || !x.decision.title.includes("Out Guy")));
});

test("a failed Start/Sit read is reported, never an all-clear", () => {
  const card = buildCard(parse(payload([ready(1, { startSit: false })])).leagues[0]);
  assert.deepEqual(card.unavailable, ["Start/Sit"]);
});

test("not ready, no team and unreadable leagues keep their card with words and the right link", () => {
  const p = parse(payload([
    notReady(1, "not_ready", "The first sync is queued.", { sync_state: "first_sync_queued", data: null }),
    notReady(2, "needs_team", "No team is saved for you in this league, so there is no lineup to check."),
    notReady(3, "unavailable", "This league's data could not be loaded."),
    notReady(4, "not_ready", "This league's data is still loading."),
  ]));
  const [a, b, c, d] = p.leagues.map(buildCard);
  assert.equal(a.status.label, "First sync queued");
  assert.equal(a.href, `/leagues?league=${LG(1)}`);
  assert.equal(b.status.label, "Team not chosen");
  assert.equal(b.href, `/leagues/${LG(2)}`);
  assert.equal(c.status.label, "Data unavailable");
  assert.equal(d.status.label, "Still loading");             // ready league, slow read: never "Up to date"
  for (const card of [a, b, c, d]) assert.deepEqual(card.decisions, []);
});

test("a degraded read says it is not fresh", () => {
  const card = buildCard(parse(payload([ready(1, { stale: true })])).leagues[0]);
  assert.equal(card.status.label, "Not fresh");
});

test("leagues rank by time to decision, then severity; not-ready before nothing-to-do", () => {
  const out = (id: number) => starter(id, { availability: { status: "OUT", sources: [] } });
  const swap = { id: "s", action: "SWAP", actionability: "ACTIONABLE", target_slot: "WR", projected_net_gain: 2.5,
    start_players: [{ id: 9, name: "B", position: "WR" }], bench_players: [{ id: 1, name: "P1", position: "WR" }] };
  const p = parse(payload([
    ready(1, { next: iso(30) }),                                                                      // nothing to do
    ready(2, { roster: [out(2)], players: { 2: { lock_status: "UNLOCKED", kickoff_utc: iso(8) } } }),  // out, 8h
    notReady(3, "needs_team", "No team."),
    ready(4, { decisions: [swap], players: { 1: { lock_status: "UNLOCKED", kickoff_utc: iso(8) }, 9: { lock_status: "UNLOCKED", kickoff_utc: iso(9) } } }), // swap, 8h, medium
    ready(5, { roster: [out(5)], players: { 5: { lock_status: "UNLOCKED", kickoff_utc: iso(1) } } }),  // out, 1h
  ]));
  const ranked = rankCards(p.leagues.map(buildCard)).map((c: CenterCard) => c.name);
  assert.deepEqual(ranked, ["League 5", "League 2", "League 4", "League 3", "League 1"]);
});

test("the cap is explicit", () => {
  const view = buildActionCenter(parse(payload([ready(1)], [league(2), league(3)])));
  assert.equal(view.moreLabel, "2 more leagues not checked here");
  assert.equal(view.more.length, 2);
  assert.equal(buildActionCenter(parse(payload([ready(1)], [league(2)]))).moreLabel, "1 more league not checked here");
  assert.equal(buildActionCenter(parse(payload([ready(1)]))).moreLabel, null);
});

test("each card shows at most the card limit and counts the rest", () => {
  const roster = [1, 2, 3, 4, 5].map((id) => starter(id, { availability: { status: "OUT", sources: [] } }));
  const card = buildCard(parse(payload([ready(1, { roster })])).leagues[0]);
  assert.equal(card.decisions.length, CARD_DECISIONS);
  assert.ok(card.more && card.more.count === 2 && card.more.href === `/l/${LG(1)}/start-sit?week=7&team=3`);
});

test("times are words, in UTC, and never invented", () => {
  assert.equal(untilText(T0 + 90 * 60000, T0), "in 1h 30m");
  assert.equal(untilText(T0 + 3 * 864e5, T0), "in 3d");
  assert.equal(untilText(T0 - 1000, T0), "passed");
  assert.equal(kickoffText(Date.parse("2026-10-25T17:00:00Z")), "Sun, Oct 25, 17:00 UTC");
  const card = buildCard(parse(payload([ready(1)])).leagues[0]);
  assert.equal(card.nextKickoff, null);
  assert.equal(card.deadline, null);
});

test("the evaluation time comes from the backend, never the render", () => {
  assert.equal(parse(payload([])).generatedAt, T0);
  assert.equal(parse({ ...payload([]), generated_at: undefined }).generatedAt, null);
});

test("a busy server is an explicit try-again, never an error page or an all-clear", () => {
  const busy = centerProblem({ status: 503, error: "Your leagues are being checked for other requests right now." });
  assert.equal(busy.tone, "caution");
  assert.match(busy.title, /could not be checked just now/);
  assert.match(busy.body, /nothing below means there is nothing to do/);
  assert.equal(busy.retryHref, "/leagues#action-center");
  const broken = centerProblem({ status: 500, error: "Internal Server Error" });
  assert.equal(broken.tone, "negative");
  assert.match(broken.body, /^Internal Server Error /);
  assert.equal(broken.retryHref, "/leagues#action-center");
});

test("a league whose week has no projections never reads as using a provider's projections", () => {
  const withSource = (source: string | null) => {
    const raw = ready(1);
    (raw.start_sit as Record<string, unknown>).projection_source = source;
    return buildCard(parse(payload([raw])).leagues[0]);
  };
  assert.equal(withSource("ESPN").projections, "ESPN projections");
  assert.equal(withSource("Independent model").projections, "Independent model projections");
  assert.equal(withSource(NO_PROJECTIONS).projections, "no projections for this week");
  assert.equal(withSource(null).projections, null);
  assert.equal(buildCard(parse(payload([ready(1, { startSit: false })])).leagues[0]).projections, null);
  assert.equal(projectionsNote(undefined), null);
});

test("a week without projections is no projection source in the decision context, never ESPN", () => {
  const ctx = (source: string) => {
    const raw = ready(1);
    (raw.start_sit as Record<string, unknown>).projection_source = source;
    return sectionContext(parse(payload([raw])).leagues[0]);
  };
  assert.equal(ctx(NO_PROJECTIONS).projectionSource, null);
  assert.equal(ctx("Independent model").projectionSource, "ours");
  assert.equal(ctx("ESPN").projectionSource, "espn");
  assert.equal(projectionSourceOf(NO_PROJECTIONS), null);
});
