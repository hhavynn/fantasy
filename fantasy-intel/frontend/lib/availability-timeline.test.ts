import assert from "node:assert/strict";
import { test } from "node:test";
import {
  changeText, eventTimeText, formatEt, injuryImpactHref, levelBadge, licenseCredits, newestFirst, relationText, signalText,
  type TimelineEvent, type TimelinePlayer, type TimelineSource,
} from "./availability-timeline.ts";

const ev = (over: Partial<TimelineEvent> = {}): TimelineEvent => ({
  id: "nfl-7-g1-0", kind: "official_report", week: 7, at: "2026-10-23T20:00:00Z", at_basis: "provider_published",
  published_at: "2026-10-23T20:00:00Z", retrieved_at: null, date: null,
  source: { type: "official", label: "NFL injury report (via nflverse)", url: "https://example.test/injuries", urls: [] },
  title: "Week 7 NFL injury report", detail: "Game status: Out.", status: "Out", practice: { code: "DNP", raw: "Did Not Participate" },
  injury: "Ankle", team: "KC", relation: "current", replaced_by: null, after_kickoff: false, ...over,
});

const player = (over: Partial<TimelinePlayer> = {}): TimelinePlayer => ({
  espn_player_id: 101, gsis_id: "00-T101", full_name: "Ace RB", position: "RB", team: "KC", lineup_slot: "RB", is_starter: true,
  headline: { level: "out", label: "Out", text: "Out: NFL report Out.", sources: ["NFL report Out"], coverage: "listed", disagreement: null, conflicting_reports: false },
  lineup: { status: "OUT", sources: ["NFL report Out"] },
  practice: { code: "DNP", label: "Did not participate", raw: "Did Not Participate", scope: "final_report", reason: null },
  change: null, high_signal: false,
  game: { week: 7, opponent: "BUF", kickoff: "2026-10-25T17:00:00Z", started: false, on_bye: false },
  freshness: null, events: [], ...over,
});

test("times are shown in US Eastern with the zone, whatever the server's zone", () => {
  assert.equal(formatEt("2026-10-23T20:00:00Z"), "Oct 23, 4:00 PM EDT");
  assert.equal(formatEt("2026-11-10T20:00:00Z"), "Nov 10, 3:00 PM EST");
  assert.equal(formatEt("2026-10-23T20:00:00"), "Oct 23, 4:00 PM EDT");   // a naive time is UTC
  assert.equal(formatEt(null), null);
  assert.equal(formatEt("not a time"), null);
});

test("an event says what its time means and never invents one", () => {
  assert.equal(eventTimeText(ev()), "Published Oct 23, 4:00 PM EDT");
  assert.match(eventTimeText(ev({ kind: "provider_status", at_basis: "retrieved" }), "Sleeper"), /^Synced .* \(Sleeper publishes no time\)$/);
  assert.equal(eventTimeText(ev({ at: null, at_basis: "week_only" })), "Week 7 (no time published)");
  assert.equal(eventTimeText(ev({ at: null, at_basis: "reported_date", date: "2026-10-19" })), "Reported Oct 19 (date only)");
});

test("superseded and earlier rows name what replaced them", () => {
  const cur = ev();
  const old = ev({ id: "nfl-7-g1-1", at: "2026-10-23T19:30:00Z", relation: "superseded", replaced_by: cur.id });
  const w6 = ev({ id: "nfl-6-g1-0", week: 6, relation: "earlier_week", replaced_by: cur.id });
  const all = [w6, old, cur];
  assert.equal(relationText(cur, all), "Current report");
  assert.equal(relationText(old, all), "Superseded by the Oct 23, 4:00 PM EDT row");
  assert.equal(relationText(w6, all), "Replaced by the week 7 report");
  assert.equal(relationText(ev({ relation: "earlier_week" }), all), "Not on any later report");
  assert.match(relationText(ev({ relation: "conflicting" }), all)!, /Conflicts/);
  assert.match(relationText(ev({ kind: "third_party", relation: "supplements" }), all)!, /never replaces/);
  assert.deepEqual(newestFirst(all).map((e) => e.id), [cur.id, old.id, w6.id]);
});

test("a dropped listing is never read as healthy", () => {
  assert.equal(changeText({ kind: "dropped_off_report", from: "Questionable / LP", to: null }),
    "Off the NFL report (was Questionable / LP): unknown, not confirmed healthy");
  assert.equal(changeText({ kind: "status_change", from: "Questionable / LP", to: "Out / DNP" }), "NFL report changed: Questionable / LP → Out / DNP");
  assert.equal(changeText(null), null);
});

test("signal lines fall back to a return or a provider-only designation", () => {
  const back = player({ change: null, events: [ev({ kind: "roster_status", relation: "current", return_to_play: true, title: "Back on the active roster" })] });
  assert.equal(signalText(back), "Back on the active NFL roster this week");
  const prov = player({ headline: { ...player().headline, level: "questionable", label: "Questionable", coverage: "not_listed" } });
  assert.equal(signalText(prov), "Questionable by the provider; no NFL report yet");
});

test("injury impact links anchor only designated players", () => {
  assert.equal(injuryImpactHref(player()), "/injury-impact#inj-00-T101");
  assert.equal(injuryImpactHref(player({ headline: { ...player().headline, level: "unknown" } })), "/injury-impact");
  assert.equal(injuryImpactHref(player({ gsis_id: null })), "/injury-impact");
  assert.equal(levelBadge("unknown"), null);
  assert.equal(levelBadge("ir"), "INJURY_RESERVE");
});

test("each licence the sources carry is credited once; sources without one add none (#186)", () => {
  const cc = { name: "CC BY 4.0", url: "https://creativecommons.org/licenses/by/4.0/", source_url: "https://github.com/nflverse",
    credit: "Injury, roster and schedule data from nflverse (CC BY 4.0), derived and reformatted by Fantasy Intel." };
  const src = (key: string, license?: TimelineSource["license"]): TimelineSource => ({ key, type: "official", label: key,
    url: null, available: true, retrieved_at: null, publication_time: "missing", text: "", license });
  assert.deepEqual(licenseCredits([src("nfl_report", cc), src("provider", null), src("schedule", cc)]), [cc]);
  assert.deepEqual(licenseCredits([src("provider", null), src("legacy")]), []);
});
