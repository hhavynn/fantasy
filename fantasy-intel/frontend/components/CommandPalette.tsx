"use client";

import { useRouter } from "next/navigation";
import { type KeyboardEvent, useCallback, useEffect, useId, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { createPortal } from "react-dom";
import {
  type PaletteItem, type SearchPlayer, buildSections, flattenSections, pageItems, playerItems, pushRecent, readLeagueRecents, searchSrc,
  writeLeagueRecents,
  wrapIndex,
} from "@/lib/palette";
import type { Provider } from "@/lib/labels";
import { withTeam } from "@/lib/team-perspective";
import { leagueRouteHref, withLeague } from "@/lib/league-scope";
import { useLeagueId } from "./useAppPathname";
import { useTeam } from "./useTeam";
import { useHiddenHrefs, useNavGroups } from "./NavVisibility";
import styles from "./CommandPalette.module.css";

const DEBOUNCE_MS = 150;
const NO_PLAYERS: SearchPlayer[] = [];

/** This league's recents (`fi:palette-recent:v1:<lg>`); none outside a league. Storage can be blocked: then none. */
function readRecents(league: string | null): PaletteItem[] {
  try {
    return readLeagueRecents(window.localStorage, league);
  } catch {
    return [];
  }
}

function writeRecents(league: string | null, items: PaletteItem[]) {
  try {
    writeLeagueRecents(window.localStorage, league, items);
  } catch {
    // Storage can be blocked (private windows, site settings); recents are a convenience.
  }
}

const noopSubscribe = () => () => {};
const isMac = () => /Mac|iPhone|iPad/.test(navigator.userAgent);

function isEditable(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

/**
 * The app bar's search trigger and the palette dialog it opens. Mounted once, in the root layout, which passes the
 * league's provider and the league it belongs to (`providerLeague`, null on the classic site), so player results name
 * roster states in that provider's terms. The layout re-renders after a client-side league switch (ScopeRefresh), so
 * for a moment the prop can still describe the previous league: results are worded with the provider only when it
 * belongs to the league they came from, and with provider-neutral words until then.
 */
export function CommandPalette({ provider = "espn", providerLeague = null }: { provider?: Provider; providerLeague?: string | null }) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const returnTo = useRef<HTMLElement | null>(null);
  const mac = useSyncExternalStore(noopSubscribe, isMac, () => true);
  // The analyzed team (`?team=`): a team-scoped destination opens for the same team, as the nav links do.
  const team = useTeam();

  const show = useCallback(() => {
    returnTo.current = document.activeElement instanceof HTMLElement ? document.activeElement : trigger.current;
    setOpen(true);
  }, []);
  const close = useCallback(() => {
    setOpen(false);
    const back = returnTo.current && document.contains(returnTo.current) ? returnTo.current : trigger.current;
    back?.focus();
  }, []);

  // Ctrl/Cmd+K toggles from anywhere; "/" opens when the focus is not in a text field.
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "k") {
        e.preventDefault();
        if (open) close(); else show();
      } else if (e.key === "/" && !e.metaKey && !e.ctrlKey && !e.altKey && !open && !isEditable(e.target)) {
        e.preventDefault();
        show();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, show, close]);

  return (
    <>
      <button ref={trigger} type="button" className={styles.trigger} aria-haspopup="dialog" aria-expanded={open}
              aria-keyshortcuts="Control+K Meta+K /" onClick={show}>
        <svg className={styles.triggerIcon} width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
          <circle cx="7" cy="7" r="4.6" fill="none" stroke="currentColor" strokeWidth="1.5" />
          <path d="M10.5 10.5L14 14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        </svg>
        <span className={styles.triggerLabel}>Search players, pages…</span>
        <kbd className={styles.kbd} aria-hidden="true">{mac ? "⌘K" : "Ctrl K"}</kbd>
      </button>
      {open && createPortal(<PaletteDialog onClose={close} team={team} provider={provider} providerLeague={providerLeague} />, document.body)}
    </>
  );
}

/**
 * `key`: the league and query the players answer, so another league's results are never shown for the same text;
 * `league`: the league they came from, which decides whether the provider prop may word them.
 */
type PlayerResult = { key: string; league: string | null; players: SearchPlayer[]; failed: boolean };

function PaletteDialog({ onClose, team, provider, providerLeague }: {
  onClose: () => void; team: string | null; provider: Provider; providerLeague: string | null;
}) {
  const league = useLeagueId();
  const router = useRouter();
  const uid = useId();
  const listId = `${uid}-list`;
  const dialog = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [recents] = useState<PaletteItem[]>(() => readRecents(league));
  const [result, setResult] = useState<PlayerResult>({ key: "", league: null, players: [], failed: false });
  const [activeId, setActiveId] = useState<string | null>(null);

  const q = query.trim();
  const key = `${league ?? ""} ${q}`;
  const pending = q !== "" && result.key !== key;
  const raw = result.key === key ? result.players : NO_PLAYERS;
  // The provider prop words the results only when it describes the league that produced them; else neutral words.
  const wording = providerLeague === result.league ? provider : null;
  const players = useMemo(() => playerItems(raw, wording), [raw, wording]);
  const groups = useNavGroups();
  const pages = useMemo(() => pageItems(groups), [groups]);
  const hidden = useHiddenHrefs();
  const sections = useMemo(() => buildSections(q, pages, players, recents, pending, hidden),
    [q, pages, players, recents, pending, hidden]);
  const flat = useMemo(() => flattenSections(sections), [sections]);
  const activeIndex = Math.max(0, flat.findIndex((i) => i.id === activeId));
  const active = flat[activeIndex] ?? null;

  // Player search: debounced, and a newer keystroke aborts the request still in flight. On a league page the request
  // goes to /l/<lg>/api/search, so the results are that league's rosters, never the classic league's.
  useEffect(() => {
    if (!q) return;
    const ctrl = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const res = await fetch(leagueRouteHref(searchSrc(q), league), { signal: ctrl.signal });
        if (!res.ok) throw new Error(String(res.status));
        const body = (await res.json()) as { players: SearchPlayer[] };
        setResult({ key, league, players: body.players, failed: false });
      } catch {
        if (!ctrl.signal.aborted) setResult({ key, league, players: [], failed: true });
      }
    }, DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      ctrl.abort();
    };
  }, [q, league, key]);

  // Lock page scroll while open (the stable gutter in globals.css keeps the layout from shifting).
  useEffect(() => {
    const html = document.documentElement;
    const prev = html.style.overflow;
    html.style.overflow = "hidden";
    return () => {
      html.style.overflow = prev;
    };
  }, []);

  useEffect(() => {
    input.current?.focus();
  }, []);

  useEffect(() => {
    if (active) document.getElementById(`${uid}-${active.id}`)?.scrollIntoView({ block: "nearest" });
  }, [active, uid]);

  const choose = useCallback((item: PaletteItem) => {
    writeRecents(league, pushRecent(readRecents(league), item));
    onClose();
    router.push(withLeague(withTeam(item.href, team), league));
  }, [league, onClose, router, team]);

  const move = (delta: number) => {
    const next = flat[wrapIndex(activeIndex, delta, flat.length)];
    if (next) setActiveId(next.id);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    switch (e.key) {
      case "ArrowDown": e.preventDefault(); move(1); break;
      case "ArrowUp": e.preventDefault(); move(-1); break;
      case "Home": if (e.target === input.current && !query) { e.preventDefault(); if (flat[0]) setActiveId(flat[0].id); } break;
      case "End": if (e.target === input.current && !query) { e.preventDefault(); const last = flat[flat.length - 1]; if (last) setActiveId(last.id); } break;
      case "Enter": if (e.target === input.current && active) { e.preventDefault(); choose(active); } break;
      case "Escape": e.preventDefault(); e.stopPropagation(); onClose(); break;
      case "Tab": {
        const stops = Array.from(dialog.current?.querySelectorAll<HTMLElement>("input, button:not([disabled])") ?? []);
        if (stops.length === 0) break;
        const i = stops.indexOf(document.activeElement as HTMLElement);
        const next = e.shiftKey ? (i <= 0 ? stops.length - 1 : i - 1) : (i === stops.length - 1 ? 0 : i + 1);
        e.preventDefault();
        stops[next].focus();
        break;
      }
    }
  };

  const status = pending ? "Searching players"
    : result.failed && q ? "Player search is unavailable"
    : flat.length === 0 ? "No results"
    : `${flat.length} ${flat.length === 1 ? "result" : "results"}`;

  return (
    <div className={styles.backdrop} onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div ref={dialog} className={styles.palette} role="dialog" aria-modal="true" aria-label="Search" onKeyDown={onKeyDown}
           onMouseDown={(e) => { if (!(e.target instanceof HTMLInputElement)) e.preventDefault(); }}>
        <div className={styles.field}>
          <svg className={styles.fieldIcon} width="18" height="18" viewBox="0 0 16 16" aria-hidden="true">
            <circle cx="7" cy="7" r="4.6" fill="none" stroke="currentColor" strokeWidth="1.5" />
            <path d="M10.5 10.5L14 14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
          <input
            ref={input}
            className={styles.input}
            type="text"
            role="combobox"
            aria-label="Search players and pages"
            aria-expanded="true"
            aria-controls={listId}
            aria-autocomplete="list"
            aria-activedescendant={active ? `${uid}-${active.id}` : undefined}
            autoComplete="off"
            autoCorrect="off"
            autoCapitalize="off"
            spellCheck={false}
            enterKeyHint="go"
            placeholder="Search players, pages…"
            value={query}
            onChange={(e) => { setQuery(e.target.value); setActiveId(null); }}
          />
          <button type="button" className={styles.close} onClick={onClose} aria-label="Close search">Esc</button>
        </div>
        <div className={styles.body}>
          {flat.length > 0 && (
            <div id={listId} role="listbox" aria-label="Results" className={styles.list}>
              {sections.map((s) => (
                <div key={s.key} role="group" aria-labelledby={`${uid}-${s.key}`}>
                  <div id={`${uid}-${s.key}`} className={styles.group}>{s.title}</div>
                  {s.items.map((item) => (
                    <div
                      key={item.id}
                      id={`${uid}-${item.id}`}
                      role="option"
                      aria-selected={item.id === active?.id}
                      className={styles.option}
                      onMouseMove={() => { if (item.id !== active?.id) setActiveId(item.id); }}
                      onClick={() => choose(item)}
                    >
                      <span className={styles.label}>{item.label}</span>
                      {item.hint && <span className={styles.hint}>{item.hint}</span>}
                    </div>
                  ))}
                </div>
              ))}
            </div>
          )}
          {flat.length === 0 && !pending && (
            <p className={styles.note}>{q ? `No pages or players match “${q}”.` : "Nothing to show."}</p>
          )}
          {q !== "" && pending && <p className={styles.note}>Searching players…</p>}
          {q !== "" && !pending && result.failed && (
            <p className={styles.note}>Player search is unavailable right now. Pages and actions still work.</p>
          )}
        </div>
        <div className={styles.foot} aria-hidden="true">
          <span><kbd>↑</kbd><kbd>↓</kbd> move</span>
          <span><kbd>↵</kbd> open</span>
          <span><kbd>Esc</kbd> close</span>
        </div>
        <p className={styles.status} role="status" aria-live="polite">{status}</p>
      </div>
    </div>
  );
}
