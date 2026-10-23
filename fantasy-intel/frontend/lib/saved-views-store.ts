// The browser store behind components/SavedViews.tsx (issue #200): one per league and tool, shared by every mounted
// panel of that page. No React and no `@/` imports, so node --test covers it with an in-memory storage.
//
// Another tab can change the same key at any time. While a panel is subscribed, the `storage` event reloads it; while
// none is (the page was left), that event is not listened for, so the first subscriber after an idle period always
// re-reads storage. Every save or delete also re-reads storage first and applies the change to what is there now, so it
// never writes back a stale list over another tab's views.

import { readSavedViews, writeSavedViews, type SavedView, type ViewTool } from "./saved-views.ts";

export type ViewSnapshot = { views: SavedView[]; ready: boolean; available: boolean };
export const EMPTY_SNAPSHOT: ViewSnapshot = Object.freeze({ views: [], ready: false, available: true }) as ViewSnapshot;

export type ViewStoreEnv = {
  /** The device storage; may throw (blocked site data). */
  storage: () => Pick<Storage, "getItem" | "setItem" | "removeItem">;
  /** Listen for another tab's storage changes (the changed key, or null for a clear); returns the unsubscribe. */
  listen: (onChange: (key: string | null) => void) => () => void;
};

export type ViewStore = {
  getSnapshot: () => ViewSnapshot;
  subscribe: (listener: () => void) => () => void;
  /**
   * Apply `change` to the views in storage now (not to the possibly stale snapshot) and write the result. `change`
   * returns null to leave storage as it is. `written` is true only when the new list reached storage.
   */
  update: (change: (latest: SavedView[]) => SavedView[] | null) => { applied: boolean; written: boolean };
};

export function createViewStore(league: string, tool: ViewTool, key: string, env: ViewStoreEnv): ViewStore {
  let snapshot: ViewSnapshot = EMPTY_SNAPSHOT;
  const listeners = new Set<() => void>();
  let stopListening: (() => void) | null = null;
  const emit = () => listeners.forEach((l) => l());

  const read = (): { views: SavedView[]; available: boolean } => {
    try {
      return readSavedViews(env.storage(), league, tool);
    } catch {
      return { views: [], available: false };
    }
  };
  const load = () => {
    snapshot = { ...read(), ready: true };
    emit();
  };

  return {
    getSnapshot: () => snapshot,
    subscribe(listener) {
      listeners.add(listener);
      if (listeners.size === 1) {
        // First subscriber after an idle period: changes made meanwhile in another tab were not heard, so re-read.
        stopListening = env.listen((changed) => { if (changed === key || changed === null) load(); });
        load();
      }
      return () => {
        listeners.delete(listener);
        if (listeners.size === 0) {
          stopListening?.();
          stopListening = null;
        }
      };
    },
    update(change) {
      const latest = read();
      const next = change(latest.views);
      if (next === null) {
        snapshot = { ...latest, ready: true };
        emit();
        return { applied: false, written: false };
      }
      let written = false;
      try {
        written = writeSavedViews(env.storage(), league, tool, next);
      } catch {
        written = false;
      }
      snapshot = { views: next, ready: true, available: written };
      emit();
      return { applied: true, written };
    },
  };
}
