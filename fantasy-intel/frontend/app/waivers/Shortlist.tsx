"use client";

import { createContext, useContext, useState, useSyncExternalStore, type ReactNode } from "react";
import { readShortlist, shortlistKey, toggleShortlist, writeShortlist } from "@/lib/waiver-shortlist";
import styles from "./waivers.module.css";

type Snapshot = { ids: number[]; ready: boolean; available: boolean };

/** One store per mounted league boundary; no server singleton or cross-request browser state. */
function createStore(leagueId: string | null) {
  const initial: Snapshot = { ids: [], ready: false, available: true };
  let snapshot = initial;
  const listeners = new Set<() => void>();
  const emit = () => listeners.forEach((listener) => listener());
  const load = () => {
    try {
      snapshot = { ...readShortlist(window.localStorage, leagueId), ready: true };
    } catch {
      // Accessing the localStorage property itself can throw in restricted browsers.
      snapshot = { ids: [], ready: true, available: false };
    }
    emit();
  };
  return {
    getSnapshot: () => snapshot,
    getServerSnapshot: () => initial,
    subscribe(listener: () => void) {
      listeners.add(listener);
      if (!snapshot.ready) load();
      const changed = (event: StorageEvent) => {
        if (event.key === shortlistKey(leagueId) || event.key === null) load();
      };
      window.addEventListener("storage", changed);
      return () => { listeners.delete(listener); window.removeEventListener("storage", changed); };
    },
    toggle(id: number) {
      if (!snapshot.ready || !shortlistKey(leagueId)) return;
      const ids = toggleShortlist(snapshot.ids, id);
      let available = false;
      try { available = writeShortlist(window.localStorage, leagueId, ids); } catch { /* session state remains usable */ }
      snapshot = { ids, ready: true, available: snapshot.available && available };
      emit();
    },
  };
}

type Value = Snapshot & { leagueId: string | null; toggle: (id: number) => void };
const ShortlistContext = createContext<Value | null>(null);

/** The shortlist of the mounted league boundary, or null outside a ShortlistProvider. */
export function useShortlist(): Value | null {
  return useContext(ShortlistContext);
}

/** Parent keys this provider by canonical server league ID, resetting state on every scope transition. */
export function ShortlistProvider({ leagueId, children }: { leagueId: string | null; children: ReactNode }) {
  const [store] = useState(() => createStore(leagueId));
  const snapshot = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getServerSnapshot);
  return <ShortlistContext.Provider value={{ ...snapshot, leagueId, toggle: store.toggle }}>{children}</ShortlistContext.Provider>;
}

export function SavePlayer({ id, name }: { id: number; name: string }) {
  const value = useContext(ShortlistContext);
  if (!value) return null;
  const saved = value.ids.includes(id);
  return (
    <button type="button" className={styles.save} disabled={!value.ready || !shortlistKey(value.leagueId)}
      aria-pressed={saved} aria-label={`${saved ? "Remove" : "Save"} ${name} ${saved ? "from" : "to"} shortlist`}
      data-player-id={id} onClick={() => value.toggle(id)}>
      {saved ? "Remove" : "Save"}
    </button>
  );
}
