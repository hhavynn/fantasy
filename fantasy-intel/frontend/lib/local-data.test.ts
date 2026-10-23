import assert from "node:assert/strict";
import test from "node:test";
import { claimPlanKey } from "./claim-plan.ts";
import { RECENT_KEY as LEAGUES_KEY } from "./league-switcher.ts";
import { KEPT_PREFERENCES, OWNED_KEYS, isOwnedKey, purgeOwnedKeys } from "./local-data.ts";
import { RAIL_KEY } from "./navigation.ts";
import { LEGACY_RECENT_KEY, pushRecent, playerItem, readLeagueRecents, recentsKey, writeLeagueRecents } from "./palette.ts";
import { readSavedViews, savedViewsKey, writeSavedViews } from "./saved-views.ts";
import { THEME_KEY } from "./theme.ts";
import { editingKey, tradeScenariosKey } from "./trade-scenarios.ts";
import { shortlistKey } from "./waiver-shortlist.ts";

const A = "lg_aaaaaaaaaaaaaaaa";

function store(entries: Record<string, string> = {}) {
  const map = new Map(Object.entries(entries));
  return {
    map,
    io: {
      get length() { return map.size; },
      key: (i: number) => [...map.keys()][i] ?? null,
      getItem: (k: string) => map.get(k) ?? null,
      setItem: (k: string, v: string) => { map.set(k, v); },
      removeItem: (k: string) => { map.delete(k); },
    },
  };
}

test("every per-league store's key and every account- or league-specific key is owned; device preferences are not", () => {
  for (const k of [savedViewsKey(A, "rankings"), recentsKey(A), shortlistKey(A), claimPlanKey(A, 2), tradeScenariosKey(A, 2), editingKey(A, 2),
    `fantasy-intel:team:${A}`, "fantasy-intel:team", LEAGUES_KEY, LEGACY_RECENT_KEY, "fantasy-intel:tab-memory"])
    assert.ok(isOwnedKey(k), String(k));
  assert.ok((OWNED_KEYS as readonly string[]).includes(LEAGUES_KEY));
  assert.deepEqual([...KEPT_PREFERENCES].sort(), [RAIL_KEY, THEME_KEY].sort());
  for (const k of [THEME_KEY, RAIL_KEY]) assert.equal(isOwnedKey(k), false, k);
});

test("sign-out removes only owned account and league data from both storages, never anything else", () => {
  const local = store({
    [savedViewsKey(A, "waivers")!]: "{}", [recentsKey(A)!]: "{}", [shortlistKey(A)!]: "[]", [claimPlanKey(A, 2)!]: "{}",
    [tradeScenariosKey(A, 2)!]: "{}", [LEGACY_RECENT_KEY]: "[]", [LEAGUES_KEY]: "[]",
    // Device preferences, other versions, look-alikes and other code's keys on the origin: kept.
    [THEME_KEY]: "dark", [RAIL_KEY]: "collapsed", "fi:other-store:v1:x": "1", "fi:saved-views:v2:x": "1",
    "xfi:saved-views:v1:x": "1", "sb-auth-token": "secret", "fantasy-intel:palette-recent-extra": "1", "fantasy-intel:teams": "1",
  });
  const session = store({ [editingKey(A, 2)!]: "s1", "fantasy-intel:tab-memory": "{}", [`fantasy-intel:team:${A}`]: "2", "fantasy-intel:team": "3", "other": "1" });
  assert.equal(purgeOwnedKeys([() => local.io, () => session.io]), 11);
  assert.deepEqual([...local.map.keys()].sort(), [
    "fantasy-intel:palette-recent-extra", RAIL_KEY, THEME_KEY, "fantasy-intel:teams", "fi:other-store:v1:x", "fi:saved-views:v2:x",
    "sb-auth-token", "xfi:saved-views:v1:x",
  ].sort());
  assert.deepEqual([...session.map.keys()], ["other"]);
});

test("account switch on one browser: what account A saved in a league is gone for account B after A signs out", () => {
  const local = store();
  writeSavedViews(local.io, A, "rankings", [{ name: "A's RBs", params: [["pos", "RB"]] }]);
  writeLeagueRecents(local.io, A, pushRecent([], playerItem({ id: 207, name: "Kilo RB", position: "RB", team: "KC", owner: "Team 2", availability: "ONTEAM", injury: null })));
  assert.equal(readSavedViews(local.io, A, "rankings").views.length, 1);
  assert.equal(readLeagueRecents(local.io, A).length, 1);
  purgeOwnedKeys([() => local.io]);   // A signs out
  // B signs in on the same browser and opens the same league.
  assert.deepEqual(readSavedViews(local.io, A, "rankings").views, []);
  assert.deepEqual(readLeagueRecents(local.io, A), []);
});

test("blocked or failing storage never throws, and one bad key does not stop the rest", () => {
  assert.equal(purgeOwnedKeys([() => { throw new Error("SecurityError"); }, () => null, () => undefined]), 0);
  const throwing = { get length(): number { throw new Error("denied"); }, key: () => null, removeItem: () => {} };
  assert.equal(purgeOwnedKeys([() => throwing]), 0);
  const s = store({ [recentsKey(A)!]: "{}", [shortlistKey(A)!]: "[]" });
  const partial = { ...s.io, get length() { return s.map.size; }, removeItem: (k: string) => { if (k === recentsKey(A)) throw new Error("x"); s.map.delete(k); } };
  assert.equal(purgeOwnedKeys([() => partial]), 1);
  assert.deepEqual([...s.map.keys()], [recentsKey(A)]);
});
