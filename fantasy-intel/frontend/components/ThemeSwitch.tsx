"use client";

import { type KeyboardEvent, useRef, useSyncExternalStore } from "react";
import { cx } from "@/lib/cx";
import { THEME_CHOICES, THEME_KEY, type ThemeChoice, parseTheme, stepTheme } from "@/lib/theme";
import styles from "./ThemeSwitch.module.css";

// The theme is the `data-theme` attribute on <html> (set before paint by the inline script in the
// root layout); this store reads and writes it, so every switch on the page stays in step.
const listeners = new Set<() => void>();

function apply(choice: ThemeChoice) {
  const root = document.documentElement;
  if (choice === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", choice);
}

let fadeTimer: ReturnType<typeof setTimeout> | undefined;

function select(choice: ThemeChoice) {
  // Fade colours for a moment (globals.css, html.theme-fading); reduced-motion users get no fade.
  const root = document.documentElement;
  root.classList.add("theme-fading");
  clearTimeout(fadeTimer);
  fadeTimer = setTimeout(() => root.classList.remove("theme-fading"), 260);
  apply(choice);
  try {
    if (choice === "system") window.localStorage.removeItem(THEME_KEY);
    else window.localStorage.setItem(THEME_KEY, choice);
  } catch {
    // Storage can be blocked; the choice still applies for this visit.
  }
  listeners.forEach((l) => l());
}

function subscribe(onChange: () => void) {
  listeners.add(onChange);
  const onStorage = (e: StorageEvent) => {
    if (e.key !== THEME_KEY && e.key !== null) return;
    apply(parseTheme(e.newValue));
    onChange();
  };
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(onChange);
    window.removeEventListener("storage", onStorage);
  };
}

const snapshot = (): ThemeChoice => parseTheme(document.documentElement.getAttribute("data-theme"));
const serverSnapshot = (): ThemeChoice => "system";

function Icon({ choice }: { choice: ThemeChoice }) {
  const common = { width: 16, height: 16, viewBox: "0 0 16 16", "aria-hidden": true as const, className: styles.icon };
  if (choice === "light") {
    return (
      <svg {...common}>
        <circle cx="8" cy="8" r="2.8" fill="none" stroke="currentColor" strokeWidth="1.4" />
        <path d="M8 1.5v1.6M8 12.9v1.6M1.5 8h1.6M12.9 8h1.6M3.4 3.4l1.1 1.1M11.5 11.5l1.1 1.1M3.4 12.6l1.1-1.1M11.5 4.5l1.1-1.1"
              stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      </svg>
    );
  }
  if (choice === "dark") {
    return (
      <svg {...common}>
        <path d="M13.2 9.6A5.6 5.6 0 0 1 6.4 2.8a5.6 5.6 0 1 0 6.8 6.8Z" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
      </svg>
    );
  }
  return (
    <svg {...common}>
      <circle cx="8" cy="8" r="5.6" fill="none" stroke="currentColor" strokeWidth="1.4" />
      <path d="M8 2.4a5.6 5.6 0 0 1 0 11.2Z" fill="currentColor" />
    </svg>
  );
}

/**
 * System, Light and Dark as one radio group. `labels` shows the words beside the icons (the menu
 * drawer on phones); in the app bar the icons stand alone and the words are the accessible names.
 */
export function ThemeSwitch({ labels = false, className }: { labels?: boolean; className?: string }) {
  const current = useSyncExternalStore(subscribe, snapshot, serverSnapshot);
  const group = useRef<HTMLDivElement>(null);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const delta = e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 0;
    if (!delta) return;
    e.preventDefault();
    const next = stepTheme(current, delta);
    select(next);
    group.current?.querySelector<HTMLElement>(`[data-choice="${next}"]`)?.focus();
  };

  return (
    <div ref={group} className={cx(styles.switch, labels && styles.labelled, className)} role="radiogroup" aria-label="Theme" onKeyDown={onKeyDown}>
      {THEME_CHOICES.map((c) => (
        <button
          key={c.value}
          type="button"
          role="radio"
          aria-checked={c.value === current}
          aria-label={labels ? undefined : `${c.label} theme`}
          title={labels ? undefined : `${c.label} theme`}
          data-choice={c.value}
          tabIndex={c.value === current ? 0 : -1}
          className={styles.option}
          onClick={() => select(c.value)}
        >
          <Icon choice={c.value} />
          {labels && <span>{c.label}</span>}
        </button>
      ))}
    </div>
  );
}
