"use client";

import { usePathname, useRouter } from "next/navigation";
import { useId, useTransition, type FormEvent } from "react";
import { ADD_PARAM, DROP_PARAM, addDropHref } from "@/lib/add-drop";
import styles from "./add-drop.module.css";

type Option = { value: string; label: string };

/**
 * The two-player selection for the add/drop simulator. Every add option is a player the backend lists as available
 * (free agent or on waivers), every drop option a player on this team's roster. A plain GET form, so it works without
 * JavaScript; with it, the scenario is a client navigation that keeps the scroll position, and Back undoes it.
 * Analysis only: submitting changes the page URL, never a roster.
 */
export function AddDropForm({ addOptions, dropOptions, add, drop, carried }: {
  addOptions: Option[];
  dropOptions: Option[];
  /** The scenario now shown (as option values), preselected. */
  add: string;
  drop: string;
  /** The page's other query parameters (team, view, filters), kept on submit. */
  carried: [string, string][];
}) {
  const router = useRouter();
  const pathname = usePathname();
  const id = useId();
  const [pending, start] = useTransition();
  const submit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const data = new FormData(e.currentTarget);
    const pick = (k: string) => {
      const v = data.get(k);
      return typeof v === "string" && /^-?\d{1,12}$/.test(v) ? Number(v) : null;
    };
    const pair = { add: pick(ADD_PARAM), drop: pick(DROP_PARAM) };
    start(() => router.push(addDropHref(pathname, new URLSearchParams(carried), pair), { scroll: false }));
  };
  return (
    <form className={styles.form} method="get" action="#add-drop" onSubmit={submit} aria-busy={pending || undefined}>
      {carried.map(([k, v], i) => <input key={`${k}-${i}`} type="hidden" name={k} value={v} />)}
      <div className={styles.field}>
        <label htmlFor={`${id}-add`}>Add</label>
        <select id={`${id}-add`} name={ADD_PARAM} defaultValue={add}>
          <option value="">Nobody (drop only)</option>
          {addOptions.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
      </div>
      <div className={styles.field}>
        <label htmlFor={`${id}-drop`}>Drop</label>
        <select id={`${id}-drop`} name={DROP_PARAM} defaultValue={drop}>
          <option value="">Nobody (add only)</option>
          {dropOptions.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
      </div>
      <button type="submit" className={styles.submit} disabled={pending}>{pending ? "Simulating" : "Simulate"}</button>
    </form>
  );
}
