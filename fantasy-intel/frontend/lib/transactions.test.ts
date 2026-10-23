import assert from "node:assert/strict";
import { test } from "node:test";
import {
  afterText, backendPath, countsText, filterSummary, formatStamp, hasFilters, parseTxQuery, sortParams, tradeSides, txHref,
  type TxEvent, type TxLeg, type TxQuery,
} from "./transactions.ts";
import { sortHref } from "./sort.ts";

const BASE: TxQuery = { season: null, manager: null, type: null, pos: null, week: null, sort: "date", dir: "desc", cursor: null };

test("the URL query parses every filter and ignores what it does not know", () => {
  assert.deepEqual(parseTxQuery({}), BASE);
  assert.deepEqual(parseTxQuery({ manager: "3", type: "trade", pos: "D/ST", week: "5", sort: "week", cursor: "tx:9" }),
    { season: null, manager: 3, type: "trade", pos: "D/ST", week: 5, sort: "week", dir: "asc", cursor: "tx:9" });
  assert.deepEqual(parseTxQuery({ manager: "x", type: "pick", pos: "DL", week: "0", sort: "team", dir: "up" }), BASE);
  assert.equal(parseTxQuery({ week: "31" }).week, null);
  assert.equal(parseTxQuery({ sort: "date", dir: "asc" }).dir, "asc");
  assert.equal(parseTxQuery({ cursor: "x".repeat(201) }).cursor, null);
});

test("an earlier season is part of the bookmark; the latest season is the default and stays out of links (#193)", () => {
  assert.equal(parseTxQuery({ season: "2025" }).season, 2025);
  for (const bad of ["1998", "2101", "abc", "-1"]) assert.equal(parseTxQuery({ season: bad }).season, null, bad);
  const q: TxQuery = { ...BASE, season: 2025, week: 3, cursor: "tx:2" };
  assert.equal(backendPath(q), "/api/transactions?season=2025&week=3&cursor=tx%3A2&limit=50");
  assert.equal(txHref(q, { season: null, week: null }), "/transactions");             // back to the latest: first page
  assert.equal(txHref(BASE, { season: 2025 }), "/transactions?season=2025");
  assert.equal(sortHref(sortParams(q), "type", "asc"), "?season=2025&week=3&sort=type&dir=asc");
  assert.ok(hasFilters({ ...BASE, season: 2025 }));
  assert.equal(filterSummary({ ...BASE, season: 2025, type: "trade" }, []), "2025 season · Trade");
});

test("the team filter is `manager`, never the team-perspective `team` param", () => {
  assert.equal(parseTxQuery({ team: "4" }).manager, null);
  assert.equal(txHref(BASE, { manager: 4 }), "/transactions?manager=4");
});

test("the backend path carries filters, a non-default sort, the cursor and the page size", () => {
  assert.equal(backendPath(BASE), "/api/transactions?limit=50");
  assert.equal(backendPath({ ...BASE, manager: 2, type: "waiver", pos: "RB", week: 3, sort: "week", dir: "asc", cursor: "tx:1" }, 10),
    "/api/transactions?manager=2&type=waiver&position=RB&week=3&sort=week&dir=asc&cursor=tx%3A1&limit=10");
});

test("bookmarkable links: a filter change starts at the first page; paging keeps the filters", () => {
  const q: TxQuery = { ...BASE, manager: 2, type: "trade", cursor: "tx:7" };
  assert.equal(txHref(q), "/transactions?manager=2&type=trade&cursor=tx%3A7");
  assert.equal(txHref(q, { pos: "WR" }), "/transactions?manager=2&type=trade&pos=WR");
  assert.equal(txHref(q, { type: "trade" }), "/transactions?manager=2&type=trade&cursor=tx%3A7");   // unchanged filter
  assert.equal(txHref(q, { cursor: "tx:9" }), "/transactions?manager=2&type=trade&cursor=tx%3A9");
  assert.equal(txHref(q, { cursor: null }), "/transactions?manager=2&type=trade");
  assert.equal(txHref(BASE), "/transactions");
  assert.equal(txHref(q, { manager: null, type: null, pos: null, week: null }), "/transactions");
});

test("sort links keep the filters and drop the cursor", () => {
  const q: TxQuery = { ...BASE, week: 4, pos: "TE", cursor: "tx:3" };
  assert.equal(sortHref(sortParams(q), "type", "asc"), "?pos=TE&week=4&sort=type&dir=asc");
  assert.deepEqual(parseTxQuery(Object.fromEntries(new URLSearchParams("pos=TE&week=4&sort=type&dir=asc"))),
    { ...BASE, week: 4, pos: "TE", sort: "type", dir: "asc" });
});

test("filter summaries name the team, type, position and week", () => {
  const managers = [{ team_id: 2, name: "Bravo", abbrev: "B" }];
  assert.equal(filterSummary(BASE, managers), "All moves");
  assert.equal(filterSummary({ ...BASE, manager: 2, type: "free_agent", pos: "K", week: 9 }, managers), "Bravo · Free agent · K · Week 9");
  assert.equal(filterSummary({ ...BASE, manager: 7 }, managers), "Team 7");
  assert.equal(hasFilters(BASE), false);
  assert.equal(hasFilters({ ...BASE, week: 1 }), true);
});

test("counts read as a sentence without plural hedges", () => {
  assert.equal(countsText({}), "no moves");
  assert.equal(countsText({ trade: 1 }), "1 trade");
  assert.equal(countsText({ drop: 2, trade: 1, waiver: 12 }), "1 trade, 12 waiver claims and 2 drops");
});

test("stamps name their time zone and convert from UTC", () => {
  assert.equal(formatStamp("2026-10-05T18:05:00Z", "UTC", "en-US"), "Oct 5, 6:05 PM UTC");
  assert.equal(formatStamp("2026-10-05T18:05:00Z", "America/New_York", "en-US"), "Oct 5, 2:05 PM EDT");
  assert.equal(formatStamp("2026-10-05T18:05:00", "UTC", "en-US"), "Oct 5, 6:05 PM UTC");   // naive = UTC
  assert.equal(formatStamp(null), null);
  assert.equal(formatStamp("not a date"), null);
});

test("after-the-move text only from measured weeks", () => {
  assert.equal(afterText(null), null);
  assert.equal(afterText({ weeks: 0, first_week: 0, last_week: 0, on_roster: 0, started: 0, starter_points: null }), null);
  assert.equal(afterText({ weeks: 1, first_week: 4, last_week: 4, on_roster: 0, started: 0, starter_points: null }),
    "Rostered 0 of 1 stored week since (wk 4)");
  assert.equal(afterText({ weeks: 4, first_week: 5, last_week: 8, on_roster: 3, started: 2, starter_points: 31.44 }),
    "Rostered 3 of 4 stored weeks since (wk 5–8), started 2: 31.4 pts");
  assert.equal(afterText({ weeks: 2, first_week: 5, last_week: 6, on_roster: 2, started: 0, starter_points: null }),
    "Rostered 2 of 2 stored weeks since (wk 5–6), started 0");
});

const team = (id: number) => ({ team_id: id, name: `T${id}`, abbrev: null });
const leg = (pid: number, action: TxLeg["action"], from: number | null, to: number | null): TxLeg => ({
  action, espn_player_id: pid, full_name: `P${pid}`, position: "WR", pro_team: null,
  from_team: from === null ? null : team(from), to_team: to === null ? null : team(to), after: null,
});

test("a trade groups its players by the team that received them, extras last", () => {
  const e = { legs: [leg(1, "trade", 4, 1), leg(2, "trade", 1, 4), leg(3, "trade", 1, 4), leg(9, "drop", 4, null)] } as TxEvent;
  const sides = tradeSides(e);
  assert.deepEqual(sides.map((s) => [s.team?.team_id ?? null, s.legs.map((l) => l.espn_player_id)]),
    [[1, [1]], [4, [2, 3]], [null, [9]]]);
});
