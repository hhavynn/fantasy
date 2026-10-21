"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { type KeyboardEvent, useEffect, useId, useRef, useState, useTransition } from "react";
import { PlayerPhoto } from "@/components/PlayerPhoto";
import { Pos } from "@/components/ui/Pos";
import { VisuallyHidden } from "@/components/ui/VisuallyHidden";
import { cx } from "@/lib/cx";
import { compareHref, swapHref, type Side } from "@/lib/compare";
import { type SearchPlayer, wrapIndex } from "@/lib/palette";
import styles from "./compare.module.css";

const DEBOUNCE_MS = 150;

/** A player already in the URL. Names are known only once the comparison has loaded. */
export type PickerSelection = { id: number; name: string | null; position: string | null; team: string | null };

type Result = { q: string; players: SearchPlayer[]; failed: boolean };

/**
 * The two player pickers, a Swap link and a Clear link. Everything lives in the URL: choosing a player navigates
 * to /compare with the other side preserved, so a comparison is always a shareable link.
 */
export function ComparePicker({ a, b }: { a: PickerSelection | null; b: PickerSelection | null }) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const any = a !== null || b !== null;

  const pick = (side: Side, id: number) => {
    const href = side === "a" ? compareHref(id, b?.id) : compareHref(a?.id, id);
    startTransition(() => router.push(href));
  };

  return (
    <div className={styles.picker} aria-busy={pending || undefined}>
      <div className={styles.slots}>
        <PlayerSlot key={`a-${a?.id ?? "none"}`} side="a" selected={a} onPick={(id) => pick("a", id)} autoFocus={!any} />
        <PlayerSlot key={`b-${b?.id ?? "none"}`} side="b" selected={b} onPick={(id) => pick("b", id)} />
      </div>
      {any && (
        <div className={styles.pickerActions}>
          <Link className="chip" href={swapHref(a?.id, b?.id)}>
            <span aria-hidden="true">⇄ </span>Swap players
          </Link>
          <Link className="chip reset" href="/compare">Clear</Link>
        </div>
      )}
    </div>
  );
}

function PlayerSlot({ side, selected, onPick, autoFocus = false }: {
  side: Side;
  selected: PickerSelection | null;
  onPick: (id: number) => void;
  autoFocus?: boolean;
}) {
  const uid = useId();
  const inputId = `${uid}-input`;
  const listId = `${uid}-list`;
  const letter = side === "a" ? "A" : "B";
  const box = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const [editing, setEditing] = useState(false);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [result, setResult] = useState<Result>({ q: "", players: [], failed: false });
  const [active, setActive] = useState(-1);

  const showInput = selected === null || editing;
  const q = query.trim();
  const pendingSearch = q !== "" && result.q !== q;
  const players = result.q === q ? result.players : [];

  // Debounced search; a newer keystroke aborts the request still in flight (same route as the command palette).
  useEffect(() => {
    if (!q || !showInput) return;
    const ctrl = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`, { signal: ctrl.signal });
        if (!res.ok) throw new Error(String(res.status));
        const body = (await res.json()) as { players: SearchPlayer[] };
        setResult({ q, players: body.players, failed: false });
        setActive(body.players.length > 0 ? 0 : -1);
      } catch {
        if (!ctrl.signal.aborted) setResult({ q, players: [], failed: true });
      }
    }, DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      ctrl.abort();
    };
  }, [q, showInput]);

  useEffect(() => {
    if (editing) input.current?.focus();
  }, [editing]);

  const choose = (p: SearchPlayer) => {
    setOpen(false);
    setQuery("");
    setEditing(false);
    onPick(p.id);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      if (players.length === 0) return;
      e.preventDefault();
      setOpen(true);
      setActive((i) => wrapIndex(i, e.key === "ArrowDown" ? 1 : -1, players.length));
    } else if (e.key === "Enter") {
      if (open && active >= 0 && players[active]) {
        e.preventDefault();
        choose(players[active]);
      }
    } else if (e.key === "Escape") {
      if (open) {
        e.preventDefault();
        setOpen(false);
      } else if (selected && editing) {
        setEditing(false);
        setQuery("");
      }
    }
  };

  const status = !q
    ? ""
    : pendingSearch
      ? "Searching…"
      : result.failed
        ? "Search is unavailable right now."
        : players.length === 0
          ? `No players match “${q}”.`
          : `${players.length} ${players.length === 1 ? "player" : "players"} found. Use the arrow keys to choose.`;

  return (
    <div
      ref={box}
      className={cx(styles.slot, side === "a" ? styles.sideA : styles.sideB)}
      onBlur={(e) => {
        if (!box.current?.contains(e.relatedTarget as Node | null)) setOpen(false);
      }}
    >
      {showInput
        ? <label className={styles.slotLabel} htmlFor={inputId}>Player {letter}</label>
        : <span className={styles.slotLabel}>Player {letter}</span>}

      {showInput ? (
        <div className={styles.combo}>
          <input
            ref={input}
            id={inputId}
            type="search"
            className={styles.comboInput}
            role="combobox"
            aria-expanded={open && players.length > 0}
            aria-controls={listId}
            aria-autocomplete="list"
            aria-activedescendant={open && active >= 0 && players[active] ? `${uid}-opt-${players[active].id}` : undefined}
            autoComplete="off"
            spellCheck={false}
            autoFocus={autoFocus}
            placeholder={`Search a player to be ${letter}`}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setOpen(true);
              setActive(-1);
            }}
            onFocus={() => setOpen(true)}
            onKeyDown={onKeyDown}
          />
          {selected && (
            <button type="button" className={styles.ghost} onClick={() => { setEditing(false); setQuery(""); setOpen(false); }}>
              Cancel
            </button>
          )}
          {open && q && !pendingSearch && (
            players.length > 0 ? (
              <ul id={listId} role="listbox" aria-label={`Player ${letter} search results`} className={styles.popover}>
                {players.map((p, i) => (
                  <li
                    key={p.id}
                    id={`${uid}-opt-${p.id}`}
                    role="option"
                    aria-selected={i === active}
                    className={cx(styles.option, i === active && styles.optionActive)}
                    onMouseDown={(e) => e.preventDefault()}
                    onMouseEnter={() => setActive(i)}
                    onClick={() => choose(p)}
                  >
                    <PlayerPhoto id={p.id} name={p.name} size={26} />
                    <span className={styles.optionText}>
                      <span className={styles.optionName}>{p.name}</span>
                      <span className={styles.optionMeta}>
                        <Pos value={p.position} /> {p.team ?? "FA"}{p.owner ? ` · ${p.owner}` : ""}
                      </span>
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className={styles.popover} role="presentation">{result.failed ? "Search is unavailable right now." : `No players match “${q}”.`}</p>
            )
          )}
        </div>
      ) : (
        <div className={styles.picked}>
          <PlayerPhoto id={selected.id} name={selected.name} size={32} />
          <span className={styles.pickedText}>
            <span className={styles.pickedName}>{selected.name ?? `Player #${selected.id}`}</span>
            {(selected.position || selected.team) && (
              <span className={styles.optionMeta}><Pos value={selected.position} /> {selected.team ?? "FA"}</span>
            )}
          </span>
          <button type="button" className={styles.ghost} onClick={() => setEditing(true)} aria-label={`Change player ${letter}`}>
            Change
          </button>
        </div>
      )}
      <VisuallyHidden><span role="status" aria-live="polite">{status}</span></VisuallyHidden>
    </div>
  );
}
