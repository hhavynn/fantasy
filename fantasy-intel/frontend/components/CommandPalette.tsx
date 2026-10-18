"use client";

import { useRouter } from "next/navigation";
import { type KeyboardEvent, useCallback, useEffect, useId, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { createPortal } from "react-dom";
import { NAV_GROUPS } from "@/lib/navigation";
import {
  type PaletteItem, type SearchPlayer, buildSections, flattenSections, pageItems, parseRecents, playerItem, pushRecent, wrapIndex,
} from "@/lib/palette";
import { withTeam } from "@/lib/team-perspective";
import { useTeam } from "./useTeam";
import styles from "./CommandPalette.module.css";

const RECENT_KEY = "fantasy-intel:palette-recent";
const DEBOUNCE_MS = 150;
const PAGES = pageItems(NAV_GROUPS);
const NO_PLAYERS: PaletteItem[] = [];

function readRecents(): PaletteItem[] {
  try {
    return parseRecents(window.localStorage.getItem(RECENT_KEY));
  } catch {
    return [];
  }
}

function writeRecents(items: PaletteItem[]) {
  try {
    window.localStorage.setItem(RECENT_KEY, JSON.stringify(items));
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

/** The app bar's search trigger and the palette dialog it opens. Mounted once, in the root layout. */
export function CommandPalette() {
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
      {open && createPortal(<PaletteDialog onClose={close} team={team} />, document.body)}
    </>
  );
}

type PlayerResult = { q: string; players: PaletteItem[]; failed: boolean };

function PaletteDialog({ onClose, team }: { onClose: () => void; team: string | null }) {
  const router = useRouter();
  const uid = useId();
  const listId = `${uid}-list`;
  const dialog = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [recents] = useState<PaletteItem[]>(readRecents);
  const [result, setResult] = useState<PlayerResult>({ q: "", players: [], failed: false });
  const [activeId, setActiveId] = useState<string | null>(null);

  const q = query.trim();
  const pending = q !== "" && result.q !== q;
  const players = result.q === q ? result.players : NO_PLAYERS;
  const sections = useMemo(() => buildSections(q, PAGES, players, recents, pending), [q, players, recents, pending]);
  const flat = useMemo(() => flattenSections(sections), [sections]);
  const activeIndex = Math.max(0, flat.findIndex((i) => i.id === activeId));
  const active = flat[activeIndex] ?? null;

  // Player search: debounced, and a newer keystroke aborts the request still in flight.
  useEffect(() => {
    if (!q) return;
    const ctrl = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`, { signal: ctrl.signal });
        if (!res.ok) throw new Error(String(res.status));
        const body = (await res.json()) as { players: SearchPlayer[] };
        setResult({ q, players: body.players.map(playerItem), failed: false });
      } catch {
        if (!ctrl.signal.aborted) setResult({ q, players: [], failed: true });
      }
    }, DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      ctrl.abort();
    };
  }, [q]);

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
    writeRecents(pushRecent(readRecents(), item));
    onClose();
    router.push(withTeam(item.href, team));
  }, [onClose, router, team]);

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
