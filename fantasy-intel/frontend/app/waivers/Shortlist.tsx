"use client";

import Link from "next/link";
import { createContext, useContext, useState, useSyncExternalStore, type ReactNode } from "react";
import { withLeague } from "@/lib/league-scope";
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

export type ShortlistPlayer = { id: number; name: string; position: string; team: string | null };

export function WaiverShortlist({ players }: { players: ShortlistPlayer[] }) {
  const value = useContext(ShortlistContext);
  if (!value) return null;
  const loaded = new Map(players.map((player) => [player.id, player]));
  return (
    <section className={styles.shortlist} aria-label="Waiver shortlist">
      <h2>Shortlist <span className={styles.count}>{value.ids.length}</span></h2>
      <p className="sub">Saved on this device for this league. Not synced between devices.</p>
      {!shortlistKey(value.leagueId)
        ? <p>Open a league to save waiver candidates.</p>
        : !value.ready ? <p role="status">Loading saved players…</p>
          : <>
            {!value.available && <p role="status">Browser storage is unavailable. Changes last for this visit only.</p>}
            {value.ids.length === 0
              ? <p>No saved players yet. Use Save beside a waiver player to compare them here later.</p>
              : <ul className={styles.shortlistRows}>
                {value.ids.map((id) => {
                  const player = loaded.get(id);
                  const name = player?.name ?? `Player #${id}`;
                  return <li key={id}>
                    <div>
                      <Link href={withLeague(`/players?id=${id}`, value.leagueId)}>{name}</Link>
                      <span className="sub">{player
                        ? ` ${player.position}${player.team ? ` · ${player.team}` : ""}`
                        : " · Current data not loaded. This player may be outside the current filters or evaluated pool."}</span>
                    </div>
                    <SavePlayer id={id} name={name} />
                  </li>;
                })}
              </ul>}
          </>}
    </section>
  );
}
