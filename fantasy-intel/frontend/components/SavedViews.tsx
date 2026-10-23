"use client";

import { useSearchParams } from "next/navigation";
import { useId, useMemo, useState, useSyncExternalStore, type FormEvent } from "react";
import {
  MAX_NAME, removeView, sameView, saveView, savedViewsKey, viewHref, viewParamsFromUrl, viewSummary, viewTool,
  type SavedView, type SaveResult, type ViewTool,
} from "@/lib/saved-views";
import { EMPTY_SNAPSHOT, createViewStore, type ViewStore, type ViewStoreEnv } from "@/lib/saved-views-store";
import { plural } from "@/lib/plural";
import { TeamLink } from "./TeamLink";
import { useLeagueId } from "./useAppPathname";
import styles from "./SavedViews.module.css";

const browserEnv: ViewStoreEnv = {
  storage: () => window.localStorage,
  listen(onChange) {
    const changed = (e: StorageEvent) => onChange(e.key);
    window.addEventListener("storage", changed);
    return () => window.removeEventListener("storage", changed);
  },
};

/**
 * One store per league and tool, created on first use: views never move between leagues or tools. A store re-reads
 * storage when a panel subscribes after none was, and before every save or delete (lib/saved-views-store.ts), so a
 * change made in another tab meanwhile is neither hidden nor overwritten.
 */
const stores = new Map<string, ViewStore>();

function storeFor(league: string, tool: ViewTool): ViewStore {
  const key = savedViewsKey(league, tool)!;
  let store = stores.get(key);
  if (!store) {
    store = createViewStore(league, tool, key, browserEnv);
    stores.set(key, store);
  }
  return store;
}

const noStore: ViewStore = {
  getSnapshot: () => EMPTY_SNAPSHOT, subscribe: () => () => {}, update: () => ({ applied: false, written: false }),
};

function useViews(league: string | null, tool: ViewTool) {
  const store = league && savedViewsKey(league, tool) ? storeFor(league, tool) : noStore;
  const snapshot = useSyncExternalStore(store.subscribe, store.getSnapshot, () => EMPTY_SNAPSHOT);
  return { snapshot, update: store.update };
}

/** A view's filters in words, and whether the page shows it now (in words, not only by colour). */
function ViewLine({ tool, view, current }: { tool: ViewTool; view: SavedView; current: boolean }) {
  return (
    <span className={styles.what}>
      {viewSummary(tool, view.params)}
      {current && <span className={styles.here}> · open now</span>}
    </span>
  );
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
  const { snapshot, update } = useViews(league, tool);
  const { views, ready, available } = snapshot;
  const spec = viewTool(tool);
  const uid = useId();
  const [name, setName] = useState("");
  const [message, setMessage] = useState<{ text: string; tone: "ok" | "error" } | null>(null);
  const active = [...spec.presets, ...views].find((v) => sameView(v.params, current)) ?? null;

  const onSave = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    // Saved against the views in storage now (another tab may have changed them), not the list on screen.
    const out: { res?: SaveResult } = {};
    const { written } = update((latest) => {
      out.res = saveView(tool, latest, name, current);
      return out.res.ok ? out.res.views : null;
    });
    const res = out.res;
    if (!res) return;
    if (!res.ok) { setMessage({ text: res.error, tone: "error" }); return; }
    setMessage(written
      ? { text: `${res.replaced ? "Updated" : "Saved"} “${res.name}” on this device for this league.`, tone: "ok" }
      : { text: "This browser is not keeping saved views (storage is blocked), so the view lasts only for this visit.", tone: "error" });
    setName("");
  };
  const onDelete = (view: SavedView) => {
    update((latest) => removeView(latest, view.name));
    setMessage({ text: `Deleted “${view.name}”.`, tone: "ok" });
  };

  const summaryText = active ? active.name : current.length ? "Custom filters" : "Default view";
  return (
    <details className={styles.box} aria-label={`${spec.label} views`}>
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
                <ViewLine tool={tool} view={v} current={sameView(v.params, current)} />
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
                  <ViewLine tool={tool} view={v} current={sameView(v.params, current)} />
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
