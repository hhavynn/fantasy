"use client";

import { useRouter } from "next/navigation";
import { useId, useTransition, type FormEvent } from "react";
import { WHATIF_PARAM, whatIfHref } from "@/lib/what-if";
import styles from "./what-if.module.css";

/**
 * One slot's "try a move" control. Every option is a move the backend has already checked (eligibility, kickoff
 * locks, IR and taxi, no duplicates), carrying the whole new scenario. A plain GET form, so it works without
 * JavaScript; with it, the move is a client navigation that keeps the scroll position, and Back undoes it.
 */
export function WhatIfPicker({ slotLabel, options, carried }: {
  slotLabel: string;
  options: { value: string; label: string }[];
  /** The page's other query parameters (team, week, sort), kept on submit. */
  carried: [string, string][];
}) {
  const router = useRouter();
  const id = useId();
  const [pending, start] = useTransition();
  const submit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const value = new FormData(e.currentTarget).get(WHATIF_PARAM);
    if (typeof value !== "string" || !value) return;
    start(() => router.push(whatIfHref("/start-sit", new URLSearchParams(carried), value), { scroll: false }));
  };
  return (
    <form className={styles.picker} action="/start-sit" method="get" onSubmit={submit} aria-busy={pending || undefined}>
      {carried.map(([k, v], i) => <input key={`${k}-${i}`} type="hidden" name={k} value={v} />)}
      <label htmlFor={`${id}-move`} className={styles.pickerLabel}>Move into {slotLabel}</label>
      <select id={`${id}-move`} name={WHATIF_PARAM} defaultValue="" className={styles.pickerSelect} required>
        <option value="" disabled>Choose a player</option>
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
      <button type="submit" disabled={pending}>{pending ? "Updating" : "Try"}</button>
    </form>
  );
}
