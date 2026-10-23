import assert from "node:assert/strict";
import test from "node:test";
import { savedViewsKey, saveView, removeView, type SavedView, type ViewParams } from "./saved-views.ts";
import { createViewStore, type ViewStoreEnv } from "./saved-views-store.ts";

const A = "lg_aaaaaaaaaaaaaaaa";
const KEY = savedViewsKey(A, "rankings")!;
const view = (name: string, params: ViewParams): SavedView => ({ name, params });

/** One shared storage (the device) and a per-tab event bus: `external` writes as another tab would. */
function device() {
  const map = new Map<string, string>();
  const storage = { getItem: (k: string) => map.get(k) ?? null, setItem: (k: string, v: string) => { map.set(k, v); }, removeItem: (k: string) => { map.delete(k); } };
  const handlers = new Set<(key: string | null) => void>();
  const env: ViewStoreEnv = {
    storage: () => storage,
    listen: (fn) => { handlers.add(fn); return () => handlers.delete(fn); },
  };
  const otherTab = createViewStore(A, "rankings", KEY, { storage: () => storage, listen: () => () => {} });
  /** Another tab saves a view: storage changes, and this tab hears a `storage` event only if it is listening. */
  const external = (name: string, params: ViewParams) => {
    otherTab.update((latest) => { const r = saveView("rankings", latest, name, params); return r.ok ? r.views : null; });
    handlers.forEach((h) => h(KEY));
  };
  return { map, env, external, listening: () => handlers.size };
}

const names = (views: SavedView[]) => views.map((v) => v.name);

test("a tab that comes back re-reads views another tab saved while it was away", () => {
  const d = device();
  const store = createViewStore(A, "rankings", KEY, d.env);
  const stop = store.subscribe(() => {});
  store.update((latest) => [...latest, view("Mine", [["pos", "RB"]])]);
  stop();
  assert.equal(d.listening(), 0);   // left the page: no storage listener
  d.external("Theirs", [["pos", "WR"]]);   // missed: nobody listening
  assert.deepEqual(names(store.getSnapshot().views), ["Mine"]);
  const again = store.subscribe(() => {});
  assert.deepEqual(names(store.getSnapshot().views), ["Mine", "Theirs"]);
  // A save after coming back keeps the other tab's view.
  store.update((latest) => { const r = saveView("rankings", latest, "Third", [["pos", "TE"]]); return r.ok ? r.views : null; });
  assert.deepEqual(names(JSON.parse(d.map.get(KEY)!).views), ["Mine", "Theirs", "Third"]);
  again();
});

test("a save or delete applies to storage as it is now, never to a stale list", () => {
  const d = device();
  const store = createViewStore(A, "rankings", KEY, { ...d.env, listen: () => () => {} });   // events never arrive
  const stop = store.subscribe(() => {});
  store.update(() => [view("Mine", [["pos", "RB"]])]);
  d.external("Theirs", [["pos", "WR"]]);
  assert.deepEqual(names(store.getSnapshot().views), ["Mine"]);   // stale on screen
  store.update((latest) => removeView(latest, "Mine"));
  assert.deepEqual(names(JSON.parse(d.map.get(KEY)!).views), ["Theirs"]);
  assert.deepEqual(names(store.getSnapshot().views), ["Theirs"]);
  // A refused change (null) writes nothing but still shows what storage holds now.
  d.external("Later", [["pos", "QB"]]);
  assert.deepEqual(store.update(() => null), { applied: false, written: false });
  assert.deepEqual(names(store.getSnapshot().views), ["Theirs", "Later"]);
  stop();
});

test("while subscribed, another tab's change is heard; several panels share one listener", () => {
  const d = device();
  const store = createViewStore(A, "rankings", KEY, d.env);
  let calls = 0;
  const s1 = store.subscribe(() => { calls++; });
  const s2 = store.subscribe(() => {});
  assert.equal(d.listening(), 1);
  d.external("Theirs", [["pos", "WR"]]);
  assert.deepEqual(names(store.getSnapshot().views), ["Theirs"]);
  assert.ok(calls >= 2);
  s1();
  assert.equal(d.listening(), 1);
  s2();
  assert.equal(d.listening(), 0);
});

test("blocked storage: nothing is written and the snapshot says storage is unavailable", () => {
  const env: ViewStoreEnv = { storage: () => { throw new Error("denied"); }, listen: () => () => {} };
  const store = createViewStore(A, "rankings", KEY, env);
  const stop = store.subscribe(() => {});
  assert.deepEqual(store.getSnapshot(), { views: [], ready: true, available: false });
  assert.deepEqual(store.update(() => [view("Mine", [["pos", "RB"]])]), { applied: true, written: false });
  assert.equal(store.getSnapshot().available, false);
  stop();
});
