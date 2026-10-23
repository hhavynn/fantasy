"use client";

import { useSearchParams } from "next/navigation";
import { useId, useMemo, useState, useSyncExternalStore, type FormEvent } from "react";
import {
  MAX_NAME, readSavedViews, removeView, sameView, saveView, savedViewsKey, viewHref, viewParamsFromUrl, viewSummary, viewTool,
  writeSavedViews, type SavedView, type ViewTool,
} from "@/lib/saved-views";
import { plural } from "@/lib/plural";
import { TeamLink } from "./TeamLink";
import { useLeagueId } from "./useAppPathname";
import styles from "./SavedViews.module.css";

type Snapshot = { views: SavedView[]; ready: boolean; available: boolean };
const EMPTY: Snapshot = { views: [], ready: false, available: true };

type Store = {
  getSnapshot: () => Snapshot;
  subscribe: (l: () => void) => () => void;
  save: (views: SavedView[]) => boolean;
};

/** One store per league and tool, created on first use: views never move between leagues or tools. */
const stores = new Map<string, Store>();

function storeFor(league: string, tool: ViewTool): Store {
  const key = savedViewsKey(league, tool)!;
  const existing = stores.get(key);
  if (existing) return existing;
  let snapshot = EMPTY;
  const listeners = new Set<() => void>();
  const emit = () => listeners.forEach((l) => l());
  const load = () => {
    try {
      snapshot = { ...readSavedViews(window.localStorage, league, tool), ready: true };
    } catch {
      // Reading the localStorage property itself can throw in restricted browsers.
      snapshot = { views: [], ready: true, available: false };
    }
    emit();
  };
  const store: Store = {
    getSnapshot: () => snapshot,
    subscribe(listener) {
      listeners.add(listener);
      if (!snapshot.ready) load();
      const changed = (e: StorageEvent) => { if (e.key === key || e.key === null) load(); };
      window.addEventListener("storage", changed);
      return () => { listeners.delete(listener); window.removeEventListener("storage", changed); };
    },
    save(views) {
      let ok = false;
      try { ok = writeSavedViews(window.localStorage, league, tool, views); } catch { /* the visit keeps the list */ }
      snapshot = { views, ready: true, available: ok };
      emit();
      return ok;
    },
  };
  stores.set(key, store);
  return store;
}

const noStore: Store = { getSnapshot: () => EMPTY, subscribe: () => () => {}, save: () => false };

function useViews(league: string | null, tool: ViewTool): Store & { snapshot: Snapshot } {
  const store = league && savedViewsKey(league, tool) ? storeFor(league, tool) : noStore;
  const snapshot = useSyncExternalStore(store.subscribe, store.getSnapshot, () => EMPTY);
  return { ...store, snapshot };
}

/**
 * Saved views for a filterable page (issue #200): suggested views, the manager's own named views for this league, a
 * form that saves the current URL filters under a name, and a reset to the page's default. Views are device-local and
 * per league (lib/saved-views.ts); links keep the league and the analyzed team (TeamLink). Outside a league nothing is
 * saved, and the panel says so.
 */
export function SavedViews({ tool }: { tool: ViewTool }) {
  const league = useLeagueId();
  const search = useSearchParams();
  const current = useMemo(() => viewParamsFromUrl(tool, search), [tool, search]);
  const { snapshot, save } = useViews(league, tool);
  const { views, ready, available } = snapshot;
  const spec = viewTool(tool);
  const uid = useId();
  const [name, setName] = useState("");
  const [message, setMessage] = useState<{ text: string; tone: "ok" | "error" } | null>(null);
  const active = [...spec.presets, ...views].find((v) => sameView(v.params, current)) ?? null;

  const onSave = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const res = saveView(tool, views, name, current);
    if (!res.ok) { setMessage({ text: res.error, tone: "error" }); return; }
    const stored = save(res.views);
    setMessage(stored
      ? { text: `${res.replaced ? "Updated" : "Saved"} “${res.name}” on this device for this league.`, tone: "ok" }
      : { text: "This browser is not keeping saved views (storage is blocked), so the view lasts only for this visit.", tone: "error" });
    setName("");
  };
  const onDelete = (view: SavedView) => {
    save(removeView(views, view.name));
    setMessage({ text: `Deleted “${view.name}”.`, tone: "ok" });
  };

  const summaryText = active ? active.name : current.length ? "Custom filters" : "Default view";
  return (
    <details className={styles.box}>
      <summary className={styles.summary}>
        <span className={styles.title}>Views</span>
        <span className={styles.current}>{summaryText}</span>
      </summary>
      <div className={styles.body}>
        {current.length > 0 && (
          <p className={styles.now}>
            <span className={styles.label}>Showing</span> <span className={styles.what}>{viewSummary(tool, current)}</span>{" "}
            <TeamLink className="chip reset" href={spec.path} scroll={false}>Reset to default</TeamLink>
          </p>
        )}

        <div className={styles.group}>
          <h3 className={styles.heading} id={`${uid}-suggested`}>Suggested</h3>
          <ul className={styles.list} aria-labelledby={`${uid}-suggested`}>
            {spec.presets.map((v) => (
              <li key={v.name} className={styles.item}>
                <TeamLink href={viewHref(tool, v.params)} scroll={false} className={styles.open}
                  aria-current={sameView(v.params, current) ? "page" : undefined}>{v.name}</TeamLink>
                <span className={styles.what}>{viewSummary(tool, v.params)}</span>
              </li>
            ))}
          </ul>
        </div>

        <div className={styles.group}>
          <h3 className={styles.heading} id={`${uid}-mine`}>
            Saved on this device{ready && views.length > 0 ? <span className={styles.count}>{views.length} {plural(views.length, "view")}</span> : null}
          </h3>
          {!league ? (
            <p className={styles.note}>Saved views are kept per league. Open one of your leagues to save a view here.</p>
          ) : !ready ? null : views.length === 0 ? (
            <p className={styles.note}>None yet. Choose filters above, then name and save them below.</p>
          ) : (
            <ul className={styles.list} aria-labelledby={`${uid}-mine`}>
              {views.map((v) => (
                <li key={v.name} className={styles.item}>
                  <TeamLink href={viewHref(tool, v.params)} scroll={false} className={styles.open}
                    aria-current={sameView(v.params, current) ? "page" : undefined}>{v.name}</TeamLink>
                  <span className={styles.what}>{viewSummary(tool, v.params)}</span>
                  <button type="button" className={styles.delete} onClick={() => onDelete(v)} aria-label={`Delete view ${v.name}`}>
                    Delete
                  </button>
                </li>
              ))}
            </ul>
          )}
          {league && ready && !available && (
            <p className={styles.note}>This browser is blocking site storage, so saved views cannot be kept.</p>
          )}
        </div>

        {league && (
          <form className={styles.form} onSubmit={onSave} aria-label={`Save the current ${spec.label} filters`}>
            <label className={styles.field}>
              <span className={styles.label}>Save current filters as</span>
              <input type="text" name="view-name" value={name} maxLength={MAX_NAME} autoComplete="off" enterKeyHint="done"
                placeholder={current.length ? "View name" : "Choose a filter first"} disabled={current.length === 0}
                onChange={(e) => setName(e.target.value)} />
            </label>
            <button type="submit" className={styles.save} disabled={current.length === 0 || !ready}>Save view</button>
          </form>
        )}
        <p className={styles.fine}>
          A view keeps only this page&apos;s filters, never player data, and opens for the team you are analyzing.
        </p>
        <p className={message?.tone === "error" ? styles.error : styles.ok} role="status" aria-live="polite">{message?.text ?? ""}</p>
      </div>
    </details>
  );
}
