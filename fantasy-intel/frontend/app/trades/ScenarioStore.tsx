"use client";

import { createContext, useContext, useState, useSyncExternalStore, type ReactNode } from "react";
import { withLeague } from "@/lib/league-scope";
import { withTeam } from "@/lib/team-perspective";
import {
  editingKey, readTradeScenarios, scenarioAnalyzerHref, tradeScenariosKey, writeTradeScenarios, type RosterTeam, type Scenario,
} from "@/lib/trade-scenarios";

type Snapshot = { scenarios: Scenario[]; editing: string | null; ready: boolean; available: boolean };

function readEditing(leagueId: string | null, team: number | null): string | null {
  const key = editingKey(leagueId, team);
  if (!key) return null;
  try {
    const v = window.sessionStorage.getItem(key);
    return v && /^[A-Za-z0-9_-]{1,40}$/.test(v) ? v : null;
  } catch {
    return null;
  }
}

/** One store per mounted league and team (the page keys the provider by both); nothing is shared across leagues. */
function createStore(leagueId: string | null, team: number | null) {
  const initial: Snapshot = { scenarios: [], editing: null, ready: false, available: true };
  let snapshot = initial;
  const listeners = new Set<() => void>();
  const emit = () => listeners.forEach((l) => l());
  const key = tradeScenariosKey(leagueId, team);
  const load = () => {
    try {
      snapshot = { ...readTradeScenarios(window.localStorage, leagueId, team), editing: readEditing(leagueId, team), ready: true };
    } catch {
      // Accessing the localStorage property itself can throw in restricted browsers.
      snapshot = { scenarios: [], editing: null, ready: true, available: false };
    }
    if (!key) snapshot = { ...snapshot, available: true };
    emit();
  };
  return {
    getSnapshot: () => snapshot,
    getServerSnapshot: () => initial,
    subscribe(listener: () => void) {
      listeners.add(listener);
      if (!snapshot.ready) load();
      const changed = (event: StorageEvent) => {
        if (event.key === key || event.key === null) load();
      };
      window.addEventListener("storage", changed);
      return () => { listeners.delete(listener); window.removeEventListener("storage", changed); };
    },
    save(scenarios: Scenario[]) {
      if (!snapshot.ready || !key) return;
      let ok = false;
      try { ok = writeTradeScenarios(window.localStorage, leagueId, team, scenarios); } catch { /* the visit keeps the list */ }
      const editing = snapshot.editing && scenarios.some((s) => s.id === snapshot.editing) ? snapshot.editing : null;
      snapshot = { ...snapshot, scenarios, editing, available: snapshot.available && ok };
      emit();
    },
    setEditing(id: string | null) {
      const ek = editingKey(leagueId, team);
      if (!ek) return;
      try {
        if (id === null) window.sessionStorage.removeItem(ek);
        else window.sessionStorage.setItem(ek, id);
      } catch { /* this tab simply does not offer "Update" */ }
      snapshot = { ...snapshot, editing: id };
      emit();
    },
  };
}

export type ScenarioContext = Snapshot & {
  leagueId: string | null;
  team: number | null;
  /** Both a canonical league and a known team: otherwise nothing is stored. */
  scoped: boolean;
  /** The league data version this page was rendered from (signal-provenance.v1 snapshot_version), or null. */
  dataVersion: string | null;
  teams: RosterTeam[];
  firstWeek: number;
  finalWeek: number;
  save: (scenarios: Scenario[]) => void;
  setEditing: (id: string | null) => void;
};

const Ctx = createContext<ScenarioContext | null>(null);

export function useScenarios(): ScenarioContext | null {
  return useContext(Ctx);
}

/** The page keys this provider by canonical league ID and team, resetting state on every scope transition. */
export function ScenarioProvider({ leagueId, team, dataVersion, teams, firstWeek, finalWeek, children }: {
  leagueId: string | null; team: number | null; dataVersion: string | null; teams: RosterTeam[];
  firstWeek: number; finalWeek: number; children: ReactNode;
}) {
  const [store] = useState(() => createStore(leagueId, team));
  const snapshot = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getServerSnapshot);
  const scoped = tradeScenariosKey(leagueId, team) !== null;
  return (
    <Ctx.Provider value={{ ...snapshot, leagueId, team, scoped, dataVersion, teams, firstWeek, finalWeek, save: store.save,
      setEditing: store.setEditing }}>
      {children}
    </Ctx.Provider>
  );
}

/** A new scenario ID (opaque, random, URL-safe). */
export function newScenarioId(): string {
  try {
    return `s${crypto.randomUUID().replace(/-/g, "").slice(0, 12)}`;
  } catch {
    return `s${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
  }
}

/** The save time as stored (ISO-8601 UTC, whole seconds). */
export function nowIso(): string {
  return `${new Date().toISOString().slice(0, 19)}Z`;
}

/** Player and team names from the page's current rosters (a player on no roster has no name here). */
export function namer(teams: readonly RosterTeam[]) {
  const players = new Map<number, string>();
  const clubs = new Map<number, string>();
  for (const t of teams) {
    clubs.set(t.team_id, t.name);
    for (const p of t.roster) players.set(p.espn_player_id, p.name ?? `Player #${p.espn_player_id}`);
  }
  return {
    player: (id: number) => players.get(id) ?? `Player #${id}`,
    team: (id: number | null) => (id === null ? "no roster" : clubs.get(id) ?? `Team ${id}`),
  };
}

/** Where the analyzer reopens a scenario: this league, this team's perspective. */
export function analyzerLink(s: Scenario, leagueId: string, team: number | null): string {
  return withLeague(withTeam(scenarioAnalyzerHref(s), team === null ? null : String(team)), leagueId);
}
