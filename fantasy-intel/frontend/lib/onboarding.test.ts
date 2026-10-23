import assert from "node:assert/strict";
import test from "node:test";
import {
  canSkipImport, capabilitySummary, connectProblem, findExisting, hostHref, normalizeExternalId, onboardingView, parseProviders,
  privateHref, progressHref, supportWords, syncProgress,
} from "./onboarding.ts";
import { leagueState, parseMyLeague, type MyLeague } from "./my-leagues.ts";

const LG = "lg_abcdefghijklmnop";

test("every step of the journey is in the URL, and malformed URLs fall back to the first step", () => {
  assert.deepEqual(onboardingView({}), { step: "choose" });
  assert.deepEqual(onboardingView({ host: "yahoo" }), { step: "choose" });
  assert.deepEqual(onboardingView({ host: "sleeper" }), { step: "details", host: "sleeper" });
  assert.deepEqual(onboardingView({ host: ["espn", "sleeper"] }), { step: "details", host: "espn" });
  assert.deepEqual(onboardingView({ host: "espn", private: "4242", season: "2026" }), { step: "private", league: "4242", season: 2026 });
  // a private step needs a well-formed ESPN id and season; Sleeper has no private step
  assert.deepEqual(onboardingView({ host: "espn", private: "42x", season: "2026" }), { step: "details", host: "espn" });
  assert.deepEqual(onboardingView({ host: "espn", private: "4242", season: "1700" }), { step: "details", host: "espn" });
  assert.deepEqual(onboardingView({ host: "sleeper", private: "4242", season: "2026" }), { step: "details", host: "sleeper" });
  assert.deepEqual(onboardingView({ league: LG }), { step: "progress", leagueId: LG, note: null });
  assert.deepEqual(onboardingView({ league: LG, already: "1" }), { step: "progress", leagueId: LG, note: "already" });
  assert.deepEqual(onboardingView({ league: LG, joined: "1" }), { step: "progress", leagueId: LG, note: "joined" });
  assert.deepEqual(onboardingView({ league: "lg_x/../admin" }), { step: "choose" });
  // the hrefs the forms redirect to read back as the same step
  assert.deepEqual(onboardingView(Object.fromEntries(new URLSearchParams(privateHref("77", 2025).split("?")[1]))),
    { step: "private", league: "77", season: 2025 });
  assert.deepEqual(onboardingView(Object.fromEntries(new URLSearchParams(progressHref(LG, "joined").split("?")[1]))),
    { step: "progress", leagueId: LG, note: "joined" });
  assert.equal(hostHref("sleeper"), "/leagues?host=sleeper");
});

const PROVIDERS = {
  providers: [
    { provider: "sleeper", enabled: true, reason: null, capabilities: {
      projections: { support: "unsupported", note: "No projections endpoint." },
      teams: { support: "supported", note: null },
      standings: { support: "derived", note: "Ranked by wins." },
      new_thing: { support: "supported", note: "  " },
    } },
    { provider: "espn", enabled: false, reason: "Not enabled.", capabilities: { teams: { support: "disabled", note: "Not enabled." } } },
    { provider: 7 }, null,
  ],
};

test("capabilities come from the backend's registry, in canonical order, with words for each support level", () => {
  const [sleeper, espn, ...rest] = parseProviders(PROVIDERS);
  assert.equal(rest.length, 0);
  assert.deepEqual(sleeper.capabilities.map((c) => c.name), ["teams", "standings", "projections", "new_thing"]);
  assert.equal(sleeper.capabilities[3].label, "New thing");
  assert.equal(sleeper.capabilities[3].note, null);
  const sum = capabilitySummary(sleeper);
  assert.deepEqual(sum.available.map((c) => c.name), ["teams", "standings", "new_thing"]);
  assert.deepEqual(sum.limited.map((c) => c.name), ["projections"]);
  assert.equal(espn.enabled, false);
  assert.equal(espn.reason, "Not enabled.");
  assert.equal(supportWords("unsupported", "sleeper"), "Sleeper does not offer this");
  assert.match(supportWords("requires_auth", "espn"), /cookies, given with consent/);
  assert.deepEqual(parseProviders(null), []);
});

const mine = (over: Partial<MyLeague>): MyLeague => ({
  id: LG, provider: "espn", externalId: "4242", season: 2026, name: null, visibility: "public", status: "active",
  role: "admin", pinned: true, teamId: null, syncState: "ready", syncIssue: null, syncStalled: false, committedAt: null, ...over,
});

test("a league the visitor already follows is found instead of registered again", () => {
  const list = [mine({}), mine({ id: "lg_sleepersleepersleeper", provider: "sleeper", externalId: "1190000000000000001", season: 2025 })];
  assert.equal(findExisting(list, "espn", "004242", 2026)?.id, LG);
  assert.equal(findExisting(list, "espn", "4242", 2025), null);          // another season is another league on ESPN
  assert.equal(findExisting(list, "sleeper", "4242", null), null);
  assert.equal(findExisting(list, "sleeper", "1190000000000000001", 2026)?.provider, "sleeper");
  assert.equal(normalizeExternalId("000"), "0");
});

test("every connect failure has a clear title and a next action, and keeps the backend's own message", () => {
  const cases: [number | undefined, string, string][] = [
    [undefined, "Cannot reach the backend", "retry_later"],
    [401, "Sign in to use this endpoint.", "sign_in"],
    [404, "ESPN has no league with that id for that season.", "fix_input"],
    [422, "Enter an ESPN league id or the league's URL.", "fix_input"],
    [429, "You have added too many leagues today. Try again tomorrow.", "retry_later"],
    [403, "Your ESPN account does not own a team in this league. Ask a member for an invite.", "invite"],
    [403, "Sign-ups are not open yet.", "demo"],
    [403, "Only a league admin can do that.", "manage"],
    [409, "Another member's cookies already sync this league.", "invite"],
    [409, "Syncing is paused for this league: its ESPN cookies stopped working or ESPN no longer has it.", "manage"],
    [409, "A league needs at least one admin.", "manage"],
    [400, "ESPN did not accept these cookies for this league.", "cookies"],
    [409, "You already follow 25 leagues. Unpin one first.", "manage"],
    [409, "This league is public on ESPN: import it without cookies.", "fix_input"],
    [503, "ESPN could not be reached. Try again in a few minutes.", "retry_later"],
    [500, "Internal Server Error", "retry_later"],
  ];
  for (const [status, message, next] of cases) {
    const p = connectProblem(status, message, "espn");
    assert.equal(p.next, next, `${status} ${message}`);
    assert.equal(p.message, message);
    assert.ok(p.title.length > 0);
  }
  assert.match(connectProblem(404, "Sleeper has no NFL league with that id.", "sleeper").hint ?? "", /sleeper\.com/);
  // No failure is a dead end: every one names something to do next (#167).
  for (const [status, message] of cases) assert.notEqual(connectProblem(status, message, "espn").next, "none", `${status} ${message}`);
  // Sleeper switched off is not "could not be reached".
  const off = connectProblem(503, "Sleeper leagues are temporarily unavailable.", "sleeper");
  assert.equal(off.title, "Sleeper leagues are not available right now");
  assert.doesNotMatch(off.title + (off.hint ?? ""), /reached/);
});

test("the first sync reads as three steps, and the dashboard opens only once the snapshot is ready", () => {
  const queued = syncProgress(mine({ syncState: "first_sync_queued" }));
  assert.equal(queued.ready, false);
  assert.equal(queued.polling, true);
  assert.deepEqual(queued.steps.map((s) => s.state), ["done", "waiting", "todo"]);
  assert.ok(!queued.actions.includes("open"));

  const running = syncProgress(mine({ syncState: "first_sync_running" }));
  assert.deepEqual(running.steps.map((s) => s.state), ["done", "current", "todo"]);
  assert.equal(running.steps[1].label, "Read the league from ESPN");

  const retrying = syncProgress(mine({ syncState: "first_sync_queued", syncIssue: "provider_unreachable", provider: "sleeper" }));
  assert.equal(retrying.headline, "Retrying the first sync");
  assert.match(retrying.steps[1].detail ?? "", /Sleeper did not answer\. It is retried automatically/);
  assert.equal(retrying.polling, true);

  const dead = syncProgress(mine({ syncState: "first_sync_failed", syncIssue: "not_found" }));
  assert.deepEqual(dead.actions, ["refresh"]);
  assert.equal(dead.polling, false);
  assert.match(dead.steps[1].detail ?? "", /could not find this league\. It stopped/);

  const deadPrivate = syncProgress(mine({ syncState: "first_sync_failed", syncIssue: "access", visibility: "private" }));
  assert.deepEqual(deadPrivate.actions, ["cookies", "refresh"]);
  const deadPrivateViewer = syncProgress(mine({ syncState: "first_sync_failed", syncIssue: "access", visibility: "private", role: null }));
  assert.deepEqual(deadPrivateViewer.actions, ["refresh"]);

  const stuck = syncProgress(mine({ syncState: "first_sync_failed", status: "stale", visibility: "private" }));
  assert.equal(stuck.headline, "The first sync cannot finish");
  assert.deepEqual(stuck.actions, ["cookies", "invite"]);

  const ready = syncProgress(mine({ syncState: "ready" }));
  assert.equal(ready.ready, true);
  assert.deepEqual(ready.actions, ["open"]);
  assert.deepEqual(ready.steps.map((s) => s.state), ["done", "done", "done"]);

  const paused = syncProgress(mine({ syncState: "ready", status: "stale", visibility: "private" }));
  assert.equal(paused.ready, true);
  assert.deepEqual(paused.actions, ["open", "cookies"]);

  assert.deepEqual(syncProgress(mine({ syncState: "no_data" })).actions, ["refresh"]);
});

test("the sync issue is parsed as a category, and the league card says retrying or stopped", () => {
  const raw = { league_id: LG, provider: "espn", external_league_id: "1", season: 2026, sync_state: "first_sync_queued" };
  assert.equal(parseMyLeague({ ...raw, sync_issue: "access" })?.syncIssue, "access");
  assert.equal(parseMyLeague({ ...raw, sync_issue: "brand_new" })?.syncIssue, "internal");
  assert.equal(parseMyLeague({ ...raw, sync_issue: null })?.syncIssue, null);
  assert.equal(leagueState({ status: "active", syncState: "first_sync_queued", visibility: "public", syncIssue: "provider_unreachable" }).label,
    "Retrying first sync");
  assert.match(leagueState({ status: "active", syncState: "first_sync_failed", visibility: "public", syncIssue: null }).detail,
    /stopped after repeated tries; refresh to try again/);
});

test("a dormant or unavailable public league goes through import again, which is what recovers it", () => {
  assert.equal(canSkipImport({ visibility: "public", status: "active" }), true);
  assert.equal(canSkipImport({ visibility: "public", status: "stale" }), true);
  assert.equal(canSkipImport({ visibility: "public", status: "unavailable" }), false);
  assert.equal(canSkipImport({ visibility: "public", status: "dormant" }), false);
  // private leagues recover through fresh cookies on their progress step, never by re-sending cookies here
  assert.equal(canSkipImport({ visibility: "private", status: "unavailable" }), true);
});

test("a first sync overdue far beyond the worker cadence says so, offers a retry and never reads as failed (#167)", () => {
  for (const syncState of ["first_sync_queued", "first_sync_running"]) {
    const p = syncProgress(mine({ syncState, syncStalled: true }));
    assert.equal(p.headline, "The first sync is taking much longer than usual");
    assert.equal(p.tone, "warn");
    assert.equal(p.ready, false);
    assert.ok(p.actions.includes("refresh"));
    assert.ok(!p.steps.some((s) => s.state === "failed"), syncState);
    assert.match(p.steps[1].detail ?? "", /over an hour/);
  }
  // not stalled: the usual steps
  assert.equal(syncProgress(mine({ syncState: "first_sync_queued", syncStalled: false })).headline, "Your league is in the queue");
});

test("a league the provider returned with no teams is named as such and never opens (#167)", () => {
  const p = syncProgress(mine({ syncState: "first_sync_failed", syncIssue: "empty" }));
  assert.equal(p.ready, false);
  assert.match(p.steps[1].detail ?? "", /ESPN returned this league with no teams yet\. Try again once the league has its teams\./);
  assert.doesNotMatch(p.steps[1].detail ?? "", /repeated tries/);
  assert.match(leagueState({ status: "active", syncState: "first_sync_failed", visibility: "public", syncIssue: "empty", provider: "espn" }).detail,
    /no teams yet\. Refresh once the league has its teams\./);
  assert.equal(parseMyLeague({ league_id: "lg_aaaaaaaaaaaaaaaaaaaaaa", provider: "espn", external_league_id: "1", season: 2026,
    visibility: "public", sync_issue: "empty", sync_stalled: true })?.syncIssue, "empty");
});
