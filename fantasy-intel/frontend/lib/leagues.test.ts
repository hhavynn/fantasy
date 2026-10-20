import assert from "node:assert/strict";
import { test } from "node:test";
import {
  buildProviderCards, capabilityLabel, dataOrigin, defaultSeason, describeMissing, freshness, isInternalLeagueId,
  loadProblem, multileagueUiEnabled, myTeamOption, parseLeague, parseLeagues, parseProviders, parseTeams, providerCard,
  relativeTime, supportLabel, teamOptionText, validTeamId, validateConnect, NOT_ENABLED_REASON, SIGNED_OUT_REASON,
  PROVIDERS_UNAVAILABLE_REASON, type CardContext, type ProviderInfo,
} from "./leagues.ts";

const NOW = Date.parse("2026-10-16T12:00:00Z");
const ctx = (over: Partial<CardContext> = {}): CardContext => ({ flagEnabled: true, providersLoaded: true, canConnect: true, ...over });

const sleeper: ProviderInfo = {
  provider: "sleeper", enabled: true, reason: null,
  capabilities: {
    teams: { support: "supported", note: null }, rosters: { support: "supported", note: null },
    projections: { support: "unsupported", note: "Sleeper publishes no projections here" },
    private_leagues: { support: "requires_auth", note: null },
  },
};
const espn: ProviderInfo = { provider: "espn", enabled: false, reason: "ESPN public import is off in this deployment.", capabilities: {} };
const yahoo: ProviderInfo = { provider: "yahoo", enabled: false, reason: "Yahoo OAuth is not configured.", capabilities: {} };

test("multileagueUiEnabled is true only for the exact string 1", () => {
  assert.equal(multileagueUiEnabled("1"), true);
  for (const v of [undefined, "", "0", "true", "yes", " 1", "1 "]) assert.equal(multileagueUiEnabled(v), false, String(v));
});

test("capabilityLabel maps known names and still renders unknown ones", () => {
  assert.equal(capabilityLabel("scoring_rules"), "Scoring rules");
  assert.equal(capabilityLabel("free_agents"), "Free agents");
  assert.equal(capabilityLabel("dynasty_picks"), "Dynasty picks");
  assert.equal(capabilityLabel(""), "Unknown capability");
});

test("supportLabel is explicit for every support level and for unknown ones", () => {
  assert.match(supportLabel("unsupported"), /Not offered/);
  assert.match(supportLabel("requires_auth"), /credentials/);
  assert.match(supportLabel("disabled"), /Not enabled/);
  assert.match(supportLabel("weird"), /Unknown support level \(weird\)/);
});

test("describeMissing orders canonically, dedupes, and carries the provider note", () => {
  const out = describeMissing(["projections", "teams", "projections", "zzz"], {
    projections: { support: "unsupported", note: "none published" }, teams: { support: "disabled", note: null },
  });
  assert.deepEqual(out.map((m) => m.name), ["teams", "projections", "zzz"]);
  assert.equal(out[1].note, "none published");
  assert.equal(out[1].label, "Provider projections");
  assert.equal(out[2].support, "unsupported");
  assert.match(describeMissing(["rosters"], { rosters: { support: "supported", note: null } })[0].supportLabel, /Reported as unavailable/);
});

test("relativeTime: minutes, hours, days; naive timestamps are UTC; garbage is null", () => {
  assert.equal(relativeTime("2026-10-16T11:59:40Z", NOW), "just now");
  assert.equal(relativeTime("2026-10-16T11:30:00Z", NOW), "30 min ago");
  assert.equal(relativeTime("2026-10-16T11:30:00", NOW), "30 min ago");
  assert.equal(relativeTime("2026-10-16T07:00:00Z", NOW), "5h ago");
  assert.equal(relativeTime("2026-10-12T12:00:00Z", NOW), "4d ago");
  assert.equal(relativeTime("2026-10-16T12:30:00Z", NOW), "just now");
  assert.equal(relativeTime("not a date", NOW), null);
  assert.equal(relativeTime(null, NOW), null);
});

test("freshness: each status says what it is in words", () => {
  const ok = freshness({ status: "ok", last_synced_at: "2026-10-16T09:00:00Z", source_updated_at: "2026-10-15T12:00:00Z", last_error: null }, NOW);
  assert.deepEqual([ok.statusLabel, ok.tone, ok.summary], ["Up to date", "good", "Synced 3h ago."]);
  assert.deepEqual(ok.details, ["Provider data last changed 24h ago."]);

  const old = freshness({ status: "ok", last_synced_at: "2026-10-10T12:00:00Z", source_updated_at: null, last_error: null }, NOW);
  assert.deepEqual([old.statusLabel, old.tone], ["Synced, may be old", "warn"]);

  const stale = freshness({ status: "stale", last_synced_at: "2026-10-14T12:00:00Z", source_updated_at: null, last_error: null }, NOW);
  assert.deepEqual([stale.statusLabel, stale.tone], ["Stale", "warn"]);
  assert.match(stale.summary, /Last synced 2d ago/);

  const failed = freshness({ status: "failed", last_synced_at: "2026-10-16T08:00:00Z", source_updated_at: null, last_error: "Provider returned 502" }, NOW);
  assert.deepEqual([failed.statusLabel, failed.tone], ["Sync failed", "bad"]);
  assert.deepEqual(failed.details, ["Last error: Provider returned 502"]);
  assert.match(failed.summary, /Last successful sync 4h ago/);

  const never = freshness({ status: "never", last_synced_at: null, source_updated_at: null, last_error: null }, NOW);
  assert.deepEqual([never.statusLabel, never.tone], ["Not synced yet", "neutral"]);

  const noTime = freshness({ status: "ok", last_synced_at: null, source_updated_at: null, last_error: null }, NOW);
  assert.match(noTime.summary, /time was not recorded/);

  assert.equal(freshness({ status: "banana", last_synced_at: null, source_updated_at: null, last_error: null }, NOW).statusLabel, "Unknown status");
});

test("dataOrigin: fixture is labelled Fixture data and never looks live", () => {
  assert.equal(dataOrigin("fixture").label, "Fixture data");
  assert.equal(dataOrigin("fixture").tone, "warn");
  assert.equal(dataOrigin("live").label, "Live data");
  assert.match(dataOrigin("mystery").label, /Unknown/);
});

test("provider cards: the flag off disables every card with the deployment reason, even before provider data", () => {
  const cards = buildProviderCards([sleeper, espn, yahoo], ctx({ flagEnabled: false }));
  assert.deepEqual(cards.map((c) => c.provider), ["espn", "sleeper", "yahoo"]);
  for (const c of cards) {
    assert.equal(c.state, "disabled");
    assert.equal(c.reason, NOT_ENABLED_REASON);
    assert.equal(c.reasonSource, "flag");
  }
});

test("provider cards: no /v2/providers answer disables everything honestly", () => {
  for (const c of buildProviderCards([], ctx({ providersLoaded: false }))) {
    assert.equal(c.state, "disabled");
    assert.equal(c.reason, PROVIDERS_UNAVAILABLE_REASON);
  }
  // Loaded but this provider is missing from the answer: same treatment.
  assert.equal(providerCard("espn", undefined, ctx()).reasonSource, "unavailable");
});

test("provider cards: a disabled provider shows its own reason verbatim", () => {
  const cards = buildProviderCards([sleeper, espn, yahoo], ctx());
  const byId = Object.fromEntries(cards.map((c) => [c.provider, c]));
  assert.equal(byId.espn.state, "disabled");
  assert.equal(byId.espn.reason, "ESPN public import is off in this deployment.");
  assert.equal(byId.espn.reasonSource, "provider");
  assert.equal(byId.yahoo.reason, "Yahoo OAuth is not configured.");
  assert.equal(byId.sleeper.state, "enabled");
  assert.equal(byId.sleeper.reason, null);
  assert.equal(byId.sleeper.control, "league-id");
});

test("provider cards: a disabled provider without a reason still says why in words", () => {
  const card = providerCard("espn", { ...espn, reason: null }, ctx());
  assert.equal(card.state, "disabled");
  assert.match(card.reason ?? "", /ESPN is not enabled/);
});

test("provider cards: no signed-in user disables an enabled provider, but never overrides the provider's own reason", () => {
  const signedOut = ctx({ canConnect: false });
  assert.equal(providerCard("sleeper", sleeper, signedOut).reason, SIGNED_OUT_REASON);
  assert.equal(providerCard("sleeper", sleeper, signedOut).reasonSource, "signed-out");
  assert.equal(providerCard("espn", espn, signedOut).reasonSource, "provider");
  assert.equal(providerCard("sleeper", sleeper, ctx({ canConnect: false, signedOutReason: "custom" })).reason, "custom");
});

test("provider cards: an enabled Yahoo still has no control because OAuth is not offered here", () => {
  const card = providerCard("yahoo", { ...yahoo, enabled: true, reason: null }, ctx());
  assert.equal(card.state, "disabled");
  assert.equal(card.control, "none");
  assert.match(card.reason ?? "", /OAuth/);
});

test("provider cards list unavailable capabilities explicitly, including ones the adapter did not declare", () => {
  const card = providerCard("sleeper", sleeper, ctx());
  const names = card.unavailable.map((u) => u.name);
  assert.ok(names.includes("projections") && names.includes("private_leagues") && names.includes("matchups"));
  assert.ok(!names.includes("teams"));
});

test("validateConnect: digits only, season in range, trims whitespace", () => {
  assert.deepEqual(validateConnect({ leagueId: " 123456789 ", season: "2026" }), { ok: true, leagueId: "123456789", season: 2026 });
  const bad = validateConnect({ leagueId: "12ab", season: "26" });
  assert.equal(bad.ok, false);
  assert.ok(!bad.ok && bad.errors.leagueId === "A league id is digits only." && bad.errors.season);
  const empty = validateConnect({ leagueId: "", season: "" });
  assert.ok(!empty.ok && empty.errors.leagueId === "Enter the league id." && empty.errors.season === "Enter the season year.");
  assert.equal(validateConnect({ leagueId: "1".repeat(65), season: "2026" }).ok, false);
  assert.equal(validateConnect({ leagueId: "1", season: "1989" }).ok, false);
  assert.equal(validateConnect({ leagueId: "1", season: "2101" }).ok, false);
  assert.equal(validateConnect({ leagueId: "1", season: "20.5" }).ok, false);
  // ESPN cookies or tokens can never pass as a league id.
  assert.equal(validateConnect({ leagueId: "AEB%2Fxyz==", season: "2026" }).ok, false);
});

test("defaultSeason: January and February still belong to last season", () => {
  assert.equal(defaultSeason(new Date("2026-10-16T00:00:00Z")), 2026);
  assert.equal(defaultSeason(new Date("2027-01-20T00:00:00Z")), 2026);
  assert.equal(defaultSeason(new Date("2027-03-01T00:00:00Z")), 2027);
});

test("id validators match the backend's shapes", () => {
  assert.equal(isInternalLeagueId("lg_abcdefghijklmnopqrstuvwx"), true);
  assert.equal(isInternalLeagueId("lg_short"), false);
  assert.equal(isInternalLeagueId("../etc"), false);
  assert.equal(isInternalLeagueId(undefined), false);
  assert.equal(validTeamId("3"), true);
  assert.equal(validTeamId("449.l.1.t.2"), true);
  assert.equal(validTeamId(""), false);
  assert.equal(validTeamId("a b"), false);
});

const leaguePayload = {
  league_id: "lg_abcdefghijklmnopqrstuvwx", provider: "sleeper", external_league_id: "1234567890", season: 2026, name: "Fixture League",
  role: "owner", access: "public", my_team_external_id: "2",
  sync: { status: "ok", last_synced_at: "2026-10-16T09:00:00", source_updated_at: null, last_error: null },
  data_origin: "fixture", missing_capabilities: ["projections"],
  capabilities: { provider: "sleeper", enabled: true, reason: null, capabilities: { projections: { support: "unsupported", note: "n/a" } } },
};

test("parseLeague / parseLeagues keep valid leagues and drop malformed ones", () => {
  const league = parseLeague(leaguePayload);
  assert.equal(league?.name, "Fixture League");
  assert.equal(league?.data_origin, "fixture");
  assert.deepEqual(league?.missing_capabilities, ["projections"]);
  assert.deepEqual(league?.capabilities.projections, { support: "unsupported", note: "n/a" });
  assert.equal(parseLeague({ ...leaguePayload, season: "2026" }), null);
  assert.equal(parseLeague(null), null);
  assert.equal(parseLeagues({ leagues: [leaguePayload, { nope: 1 }] }).length, 1);
  assert.deepEqual(parseLeagues({}), []);
  assert.deepEqual(parseLeagues(null), []);
  const sparse = parseLeague({ league_id: "lg_x", provider: "espn", external_league_id: "1", season: 2026 });
  assert.equal(sparse?.sync.status, "never");
  assert.equal(sparse?.data_origin, "unknown");
});

test("parseProviders keeps well-formed entries only", () => {
  const out = parseProviders({ providers: [
    { provider: "espn", enabled: false, reason: "off", capabilities: { teams: { support: "disabled", note: "off" } } },
    { provider: "x" },
    "junk",
  ] });
  assert.equal(out.length, 1);
  assert.equal(out[0].reason, "off");
  assert.deepEqual(parseProviders(undefined), []);
});

test("parseTeams: provider ids stay strings, labels fall back, records and managers are shown when present", () => {
  const teams = parseTeams({ teams: [
    { external_team_id: "1", name: "Rockets", manager_display_names: ["Ann"], wins: 5, losses: 2, ties: 1 },
    { external_team_id: 2, name: null },
    { name: "no id" },
  ] });
  assert.deepEqual(teams.map((t) => t.id), ["1", "2"]);
  assert.equal(teamOptionText(teams[0]), "Rockets (Ann · 5-2-1)");
  assert.equal(teamOptionText(teams[1]), "Team 2");
});

test("myTeamOption only returns a team the provider still lists", () => {
  const teams = parseTeams({ teams: [{ external_team_id: "2", name: "Two" }] });
  assert.equal(myTeamOption({ my_team_external_id: "2" }, teams)?.label, "Two");
  assert.equal(myTeamOption({ my_team_external_id: "9" }, teams), null);
  assert.equal(myTeamOption({ my_team_external_id: null }, teams), null);
});

test("loadProblem keeps the backend detail in the message", () => {
  const nf = loadProblem("providers", { error: "Not Found", kind: "http", status: 404 });
  assert.equal(nf.tone, "caution");
  assert.match(nf.body, /Not Found/);
  const auth = loadProblem("your leagues", { error: "Invalid user assertion", kind: "http", status: 401 });
  assert.match(auth.title, /sign-in/);
  assert.match(auth.body, /Invalid user assertion/);
  const off = loadProblem("your leagues", { error: "Cannot reach the backend at x", kind: "offline" });
  assert.equal(off.tone, "negative");
  assert.equal(off.body, "The backend did not answer.");
  assert.ok(!off.body.includes("http"));
});

test("SIGNED_OUT_REASON explains that sign-in is coming, without mentioning assertions", () => {
  assert.match(SIGNED_OUT_REASON, /sign-?in/i);
  assert.doesNotMatch(SIGNED_OUT_REASON, /assertion|secret/i);
});
