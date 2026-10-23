import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";
import { claimPlanKey } from "./claim-plan.ts";
import { RECENT_KEY as LEAGUES_KEY } from "./league-switcher.ts";
import {
  ACCOUNT_MARKER_KEY, KEPT_PREFERENCES, OWNED_KEYS, OWNED_PREFIXES, isOwnedKey, purgeOwnedKeys, reconcileAccount,
} from "./local-data.ts";
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

/** One signed-in visit's data in both storages, plus what must survive. */
function visit(local: ReturnType<typeof store>, session: ReturnType<typeof store>) {
  writeSavedViews(local.io, A, "rankings", [{ name: "A's RBs", params: [["pos", "RB"]] }]);
  local.io.setItem(LEAGUES_KEY, JSON.stringify([{ id: A, name: "A private league" }]));
  local.io.setItem(claimPlanKey(A, 2)!, "{}");
  session.io.setItem(editingKey(A, 2)!, "s1");
  session.io.setItem(`fantasy-intel:team:${A}`, "2");
  local.io.setItem(THEME_KEY, "dark");
  local.io.setItem("sb-auth-token", "managed by Supabase");
}
const kept = (s: ReturnType<typeof store>) => [...s.map.keys()].sort();

test("sign out of one account, sign in to another on this browser: the first account's data is gone, even without Sign out", () => {
  const local = store();
  const session = store();
  const L = () => local.io;
  const S = () => session.io;
  assert.equal(reconcileAccount(L, S, "acct-A"), false, "first sign-in: nothing to clear");
  assert.equal(local.map.get(ACCOUNT_MARKER_KEY), "acct-A");
  visit(local, session);
  assert.equal(reconcileAccount(L, S, "acct-A"), false, "the same account keeps its data on every page");
  assert.equal(readSavedViews(local.io, A, "rankings").views.length, 1);

  // The session ends without the Sign out button (expired, revoked, or the account deleted): the next page is signed out.
  assert.equal(reconcileAccount(L, S, null), true);
  assert.deepEqual(kept(local), ["sb-auth-token", THEME_KEY].sort());
  assert.deepEqual(kept(session), []);
  assert.equal(reconcileAccount(L, S, null), false, "unmarked signed-out visits clear nothing");

  // B signs in: a clean slate marked as B's.
  assert.equal(reconcileAccount(L, S, "acct-B"), false);
  assert.equal(local.map.get(ACCOUNT_MARKER_KEY), "acct-B");
  assert.deepEqual(readSavedViews(local.io, A, "rankings").views, []);
});

test("another account signing in without a sign-out in between still clears the first account's data", () => {
  const local = store();
  const session = store();
  reconcileAccount(() => local.io, () => session.io, "acct-A");
  visit(local, session);
  assert.equal(reconcileAccount(() => local.io, () => session.io, "acct-B"), true);
  assert.deepEqual(kept(local), [ACCOUNT_MARKER_KEY, "sb-auth-token", THEME_KEY].sort());
  assert.equal(local.map.get(ACCOUNT_MARKER_KEY), "acct-B");
  assert.deepEqual(kept(session), []);
});

test("data from a visitor who never signed in is left alone, becomes the first account's, and goes at its sign-out", () => {
  const local = store();
  const session = store();
  visit(local, session);
  assert.equal(reconcileAccount(() => local.io, () => session.io, null), false);
  assert.equal(reconcileAccount(() => local.io, () => session.io, "acct-A"), false);
  assert.equal(readSavedViews(local.io, A, "rankings").views.length, 1);
  assert.equal(reconcileAccount(() => local.io, () => session.io, null), true);
  assert.deepEqual(kept(local), ["sb-auth-token", THEME_KEY].sort());
});

test("reconcileAccount never throws on blocked storage", () => {
  const boom = () => { throw new Error("SecurityError"); };
  assert.equal(reconcileAccount(boom, boom, "acct-A"), false);
  assert.equal(reconcileAccount(() => null, () => null, null), false);
  const local = store({ [ACCOUNT_MARKER_KEY]: "acct-A" });
  assert.equal(reconcileAccount(() => local.io, boom, null), true, "a blocked sessionStorage does not stop the localStorage purge");
  assert.deepEqual(kept(local), []);
});

// Every browser-storage key literal in the app must be classified, so a new store cannot outlive sign-out by accident:
// each `fi:<store>:` / `fantasy-intel:<name>` literal in app/, components/ and lib/ is either purged or a kept preference.
test("source audit: every storage key the app writes is purged at sign-out or is a kept device preference", () => {
  const root = new URL("..", import.meta.url).pathname;
  const files: string[] = [];
  const walk = (dir: string) => {
    for (const name of readdirSync(dir)) {
      const p = join(dir, name);
      if (statSync(p).isDirectory()) walk(p);
      else if (/\.(ts|tsx)$/.test(name) && !/\.test\.ts$/.test(name)) files.push(p);
    }
  };
  for (const d of ["app", "components", "lib"]) walk(join(root, d));
  const found = new Set<string>();
  for (const f of files) {
    for (const m of readFileSync(f, "utf8").matchAll(/["'`]((?:fantasy-intel|fi):[a-z0-9-]+(?::v\d+:)?)/g)) found.add(m[1]);
  }
  assert.ok(found.size >= 12, [...found].join(", "));
  const kept = KEPT_PREFERENCES as readonly string[];
  for (const key of found) {
    const purged = OWNED_PREFIXES.some((p) => p.startsWith(key) || key.startsWith(p)) || (OWNED_KEYS as readonly string[]).includes(key);
    assert.ok(purged !== kept.includes(key), `${key}: add it to OWNED_PREFIXES / OWNED_KEYS (purged at sign-out) or KEPT_PREFERENCES`);
  }
});
